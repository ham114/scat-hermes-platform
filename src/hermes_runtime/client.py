"""Hermes v0.22.3 runs protocol; not an LLM-compatible model wrapper."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

from application.context import ExecutionContext
from hermes_runtime.registry import RunBindingConflictError, RunRegistry

_TERMINAL = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})


class HermesRuntimeError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    name: str
    run_id: str
    sequence: int | None
    payload: dict[str, Any]


class HermesHTTPRuntime:
    def __init__(
        self,
        base_url: str,
        api_key: SecretStr,
        registry: RunRegistry,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        results=None,
        memory=None,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Expected an origin URL for the Hermes internal service")
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "hermes",
        }:
            raise ValueError("Plain HTTP is restricted to loopback or the internal hermes service")
        if len(api_key.get_secret_value()) < 16:
            raise ValueError("Hermes service key must contain at least 16 characters")
        self.registry = registry
        self.results = results
        self.memory = memory
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            headers={"Authorization": "Bearer " + api_key.get_secret_value()},
            timeout=httpx.Timeout(60, connect=5, read=120),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    @staticmethod
    def _headers(context: ExecutionContext) -> dict[str, str]:
        return {"X-Hermes-Session-Key": context.scope_key}

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.is_redirect:
            raise HermesRuntimeError("upstream_redirect_refused")
        if response.status_code >= 400:
            # No remote message, URL, headers or exception repr reaches a caller.
            code = {
                400: "upstream_bad_request",
                401: "upstream_auth_failed",
                422: "upstream_invalid_request",
                403: "upstream_forbidden",
                404: "upstream_not_found",
                409: "upstream_conflict",
                429: "upstream_rate_limited",
            }.get(response.status_code, "upstream_failed")
            raise HermesRuntimeError(code)

    async def _json(
        self,
        method: str,
        path: str,
        *,
        context: ExecutionContext,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        merged = self._headers(context) | (headers or {})
        try:
            response = await self._client.request(method, path, headers=merged, **kwargs)
        except httpx.TransportError:
            # POST may have reached Hermes: never silently retry a task admission.
            raise HermesRuntimeError(
                "upstream_unavailable", outcome_unknown=method == "POST"
            ) from None
        self._check_response(response)
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise HermesRuntimeError("invalid_upstream_json") from None
        if not isinstance(data, dict):
            raise HermesRuntimeError("invalid_upstream_json")
        return data

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/health", timeout=3)
            return response.status_code == 200 and response.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    async def create_run(
        self, context: ExecutionContext, message: str, *, request_id: UUID
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip() or len(message) > 32_000:
            raise ValueError("Message must contain 1-32000 characters")
        if not isinstance(request_id, UUID):
            raise TypeError("A stable UUID request_id is required")
        key = context.scope_key + ":" + str(request_id)
        body = {"input": message}
        if self.memory is not None:
            try:
                instructions = await self.memory.prepare(context, message, request_id)
            except ValueError:
                raise HermesRuntimeError("memory_request_conflict") from None
            if instructions:
                body["instructions"] = instructions
        data = await self._json(
            "POST",
            "/v1/runs",
            context=context,
            headers={"Idempotency-Key": key},
            json=body,
        )
        if data.get("status") not in {
            "started",
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "stopping",
            "approval_pending",
            "waiting_for_approval",
        }:
            raise HermesRuntimeError("invalid_run_admission")
        try:
            self.registry.bind(context, str(request_id), data.get("run_id"))
        except RunBindingConflictError:
            raise HermesRuntimeError("invalid_run_binding", outcome_unknown=True) from None
        if self.memory is not None:
            self.memory.admitted(context, request_id)
        return data

    async def get_run(self, context: ExecutionContext, run_id: str) -> dict[str, Any]:
        self.registry.require(context, run_id)
        data = await self._json("GET", f"/v1/runs/{run_id}", context=context)
        if data.get("run_id") != run_id:
            raise HermesRuntimeError("upstream_run_mismatch")
        return data

    async def stop_run(self, context: ExecutionContext, run_id: str) -> dict[str, Any]:
        self.registry.require(context, run_id)
        data = await self._json("POST", f"/v1/runs/{run_id}/stop", context=context, json={})
        if data.get("run_id") != run_id:
            raise HermesRuntimeError("upstream_run_mismatch")
        return data

    async def events(
        self, context: ExecutionContext, run_id: str, *, last_sequence: int = -1
    ) -> AsyncIterator[RuntimeEvent]:
        self.registry.require(context, run_id)
        if type(last_sequence) is not int or last_sequence < -1:
            raise ValueError("Invalid SSE cursor")
        headers = self._headers(context) | {"Accept": "text/event-stream"}
        if last_sequence >= 0:
            headers["Last-Event-ID"] = str(last_sequence)
        try:
            async with self._client.stream(
                "GET", f"/v1/runs/{run_id}/events", headers=headers
            ) as response:
                self._check_response(response)
                if (
                    response.headers.get("content-type", "").split(";")[0].strip()
                    != "text/event-stream"
                ):
                    raise HermesRuntimeError("invalid_event_stream")
                lines: list[str] = []
                event_id: str | None = None
                frame_bytes = 0
                async for line in response.aiter_lines():
                    frame_bytes += len(line.encode("utf-8"))
                    if frame_bytes > 262_144:
                        raise HermesRuntimeError("event_frame_too_large")
                    if not line:
                        if lines:
                            event = self._decode_event(run_id, lines, event_id)
                            if event.sequence is not None and event.sequence <= last_sequence:
                                pass  # reconnect replay; don't deliver the same event twice
                            else:
                                if event.sequence is not None:
                                    last_sequence = event.sequence
                                yield event
                                if event.name in _TERMINAL:
                                    return
                        lines, event_id, frame_bytes = [], None, 0
                    elif line.startswith("data:"):
                        value = line[5:]
                        lines.append(value.removeprefix(" "))
                    elif line.startswith("id:"):
                        event_id = line[3:].strip()
                # EOF is not a successful completion. Caller can poll or resume.
                raise HermesRuntimeError("event_stream_interrupted")
        except httpx.TransportError:
            raise HermesRuntimeError("event_stream_interrupted") from None

    @staticmethod
    def _decode_event(run_id: str, lines: list[str], event_id: str | None) -> RuntimeEvent:
        try:
            data = json.loads("\n".join(lines))
        except ValueError:
            raise HermesRuntimeError("invalid_event_json") from None
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            raise HermesRuntimeError("upstream_run_mismatch")
        name, sequence = data.get("event"), data.get("seq")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise HermesRuntimeError("invalid_event_type")
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise HermesRuntimeError("invalid_event_sequence")
        if event_id is not None:
            try:
                parsed_id = int(event_id)
            except ValueError:
                raise HermesRuntimeError("invalid_event_sequence") from None
            if parsed_id < 0 or (sequence is not None and sequence != parsed_id):
                raise HermesRuntimeError("invalid_event_sequence")
            sequence = parsed_id
        if name == "run.completed" and (
            data.get("completed") is False or data.get("partial") or data.get("interrupted")
        ):
            raise HermesRuntimeError("inconsistent_completion")
        return RuntimeEvent(name, run_id, sequence, data)
