"""Bounded HTTP execution with deadlines and conservative retry semantics."""

from __future__ import annotations

import asyncio
import ssl
import threading
from typing import Protocol
from urllib.parse import urlsplit

import certifi
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.resilience import CircuitBreaker, CircuitOpenError
from scat_model_runtime.backends.base import (
    BackendError,
    BackendHealth,
    CancelResult,
    InferenceRequest,
    InferenceResult,
)


class BentoHttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = Field(max_length=500)
    release_id: str = Field(pattern=r"^[a-z0-9._-]+:[a-z0-9][a-z0-9._-]{0,100}$")
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    first_byte_timeout_seconds: float = Field(default=120, gt=0, le=120)
    total_timeout_seconds: float = Field(default=125, gt=0, le=300)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=2_000_000)
    max_connect_retries: int = Field(default=1, ge=0, le=1)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_recovery_seconds: float = Field(default=30, gt=0, le=3600)

    @field_validator("endpoint")
    @classmethod
    def server_endpoint(cls, value):
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid server endpoint")
        _ = url.port
        return value.rstrip("/")

    @field_validator("release_id")
    @classmethod
    def immutable_release(cls, value):
        if value.rsplit(":", 1)[-1] == "latest":
            raise ValueError("latest is not an immutable release")
        return value


class TransportCodec(Protocol):
    def encode(self, request: InferenceRequest) -> bytes: ...
    def decode(self, request: InferenceRequest, raw: bytes, status: int) -> InferenceResult: ...
    def verify_metadata(self, metadata: dict, release_id: str) -> BackendHealth: ...


class BentoHttpBackend:
    def __init__(self, config: BentoHttpConfig, codec: TransportCodec, *, transport=None):
        self.config, self.codec = config, codec
        self._transport = transport
        self._closed = False
        self._lock = threading.Lock()
        self._active: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}
        self._circuit = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            recovery_timeout_seconds=config.circuit_recovery_seconds,
        )
        # Clients have separate event-loop lifetimes. Reuse only the immutable
        # trust context, retaining HTTPX's certifi roots and hostname verification.
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())

    async def _exchange(self, client, method, path, content=None):
        # HTTPX's read timeout covers first byte and each subsequent read;
        # the enclosing asyncio deadline also bounds a slow trickle response.
        async with client.stream(
            method,
            self.config.endpoint + path,
            content=content,
            headers={"content-type": "application/json", "accept-encoding": "identity"},
        ) as response:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise BackendError("invalid_response", execution_state="unknown")
            declared = response.headers.get("content-length")
            try:
                if (
                    declared is not None
                    and not 0 <= int(declared) <= self.config.max_response_bytes
                ):
                    raise BackendError("response_too_large", execution_state="unknown")
            except ValueError:
                raise BackendError("invalid_response", execution_state="unknown") from None
            body = bytearray()
            async for block in response.aiter_bytes():
                if len(body) + len(block) > self.config.max_response_bytes:
                    raise BackendError("response_too_large", execution_state="unknown")
                body.extend(block)
            return response.status_code, bytes(body)

    def _client(self):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.config.connect_timeout_seconds,
                read=self.config.first_byte_timeout_seconds,
                write=self.config.connect_timeout_seconds,
                pool=self.config.connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
            verify=self._ssl_context,
            transport=self._transport,
        )

    async def _execute(self, request, content):
        with self._lock:
            if self._closed:
                raise BackendError("closed")
            if request.request_id in self._active:
                raise BackendError("duplicate_request")
            self._active[request.request_id] = (asyncio.get_running_loop(), asyncio.current_task())
        try:
            async with asyncio.timeout(self.config.total_timeout_seconds), self._client() as client:
                for attempt in range(self.config.max_connect_retries + 1):
                    try:
                        status, raw = await self._exchange(client, "POST", "/predict", content)
                        return self.codec.decode(request, raw, status)
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                        if attempt == self.config.max_connect_retries:
                            raise BackendError("connect_failed", retryable=True) from None
                        await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise BackendError("cancelled", execution_state="unknown") from None
        except (TimeoutError, httpx.TimeoutException):
            raise BackendError("inference_timeout", execution_state="unknown") from None
        except httpx.HTTPError:
            raise BackendError("connection_lost", execution_state="unknown") from None
        finally:
            with self._lock:
                self._active.pop(request.request_id, None)

    def execute(self, request):
        if self._closed:
            raise BackendError("closed")
        content = self.codec.encode(request)
        try:
            self._circuit.before_call()
        except CircuitOpenError:
            raise BackendError("circuit_open") from None
        try:
            result = asyncio.run(self._execute(request, content))
            result.verify_identity(request)
        except BackendError as exc:
            if exc.code not in {
                "invalid_input",
                "data_unrecoverable",
                "busy",
                "duplicate_request",
                "closed",
            }:
                self._circuit.record_failure()
            else:
                self._circuit.record_success()
            raise
        except Exception:
            self._circuit.record_failure()
            raise BackendError("invalid_response", execution_state="unknown") from None
        self._circuit.record_success()
        return result

    async def _health(self):
        import json

        async with asyncio.timeout(self.config.total_timeout_seconds), self._client() as client:
            status, _ = await self._exchange(client, "GET", "/ready")
            if status != 200:
                return BackendHealth(False, "not_ready")
            status, raw = await self._exchange(client, "GET", "/metadata")
            if status != 200:
                return BackendHealth(False, "not_ready")
            return self.codec.verify_metadata(json.loads(raw), self.config.release_id)

    def health(self):
        if self._closed:
            return BackendHealth(False, "closed")
        try:
            return asyncio.run(self._health())
        except Exception:
            return BackendHealth(False, "metadata_unavailable")

    def cancel(self, request_id):
        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                return CancelResult(False, "not_started")
            loop, task = active
            loop.call_soon_threadsafe(task.cancel)
        # Closing the caller's HTTP connection cannot confirm CUDA termination.
        return CancelResult(True, "unknown", remote_termination_confirmed=False)

    def close(self):
        with self._lock:
            self._closed = True
            requests = tuple(self._active)
        for request_id in requests:
            self.cancel(request_id)
