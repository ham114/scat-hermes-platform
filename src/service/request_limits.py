"""Bounded request bodies and per-process HTTP concurrency."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from core.redis_request_limiter import RedisRequestLimiter
from service.request_context import REQUEST_ID_HEADER, normalize_request_id

_EXEMPT_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/health/dependencies"})


class RequestLimitsMiddleware:
    """Pure ASGI guard that rejects oversized bodies before endpoint parsing."""

    def __init__(
        self,
        app: Any,
        *,
        max_body_bytes: int = 8_000_000,
        max_concurrency: int = 64,
        global_limiter: RedisRequestLimiter | None = None,
    ) -> None:
        if max_body_bytes < 1 or max_concurrency < 1:
            raise ValueError("request limits must be positive")
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        self.max_concurrency = int(max_concurrency)
        self.global_limiter = global_limiter
        self._active = 0
        self._lock = asyncio.Lock()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await self._reject(scope, send, 400, "bad_request", "Invalid Content-Length")
                return
            if declared_length < 0:
                await self._reject(scope, send, 400, "bad_request", "Invalid Content-Length")
                return
            if declared_length > self.max_body_bytes:
                await self._reject(
                    scope,
                    send,
                    413,
                    "request_too_large",
                    f"Request body exceeds {self.max_body_bytes} bytes",
                )
                return

        async with self._lock:
            if self._active >= self.max_concurrency:
                await self._reject(
                    scope,
                    send,
                    429,
                    "rate_limited",
                    "Too many concurrent requests",
                    extra_headers=[(b"retry-after", b"1")],
                )
                return
            self._active += 1

        global_token: str | None = None
        renew_task: asyncio.Task[None] | None = None
        try:
            if self.global_limiter is not None:
                decision = await self.global_limiter.acquire()
                if not decision.allowed:
                    await self._reject(
                        scope,
                        send,
                        429,
                        "rate_limited",
                        (
                            "Too many concurrent requests"
                            if decision.reason == "concurrency"
                            else "Global request rate limit exceeded"
                        ),
                        extra_headers=[
                            (b"retry-after", str(decision.retry_after_seconds).encode())
                        ],
                    )
                    return
                global_token = decision.token
                if global_token is not None:
                    renew_task = asyncio.create_task(
                        self._renew_global_slot(global_token),
                        name="redis-http-limit-renewal",
                    )

            body = bytearray()
            disconnected = False
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    break
                if message["type"] != "http.request":
                    continue
                body.extend(message.get("body", b""))
                if len(body) > self.max_body_bytes:
                    await self._reject(
                        scope,
                        send,
                        413,
                        "request_too_large",
                        f"Request body exceeds {self.max_body_bytes} bytes",
                    )
                    return
                if not message.get("more_body", False):
                    break

            delivered = False

            async def replay() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    if disconnected:
                        return {"type": "http.disconnect"}
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            if renew_task is not None:
                renew_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renew_task
            if global_token is not None and self.global_limiter is not None:
                await self.global_limiter.release(global_token)
            async with self._lock:
                self._active -= 1

    async def _renew_global_slot(self, token: str) -> None:
        assert self.global_limiter is not None
        while True:
            await asyncio.sleep(self.global_limiter.renewal_interval_seconds)
            if not await self.global_limiter.renew(token):
                return

    @staticmethod
    async def _reject(
        scope: dict[str, Any],
        send: Any,
        status_code: int,
        code: str,
        message: str,
        *,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(REQUEST_ID_HEADER.lower().encode())
        request_id = normalize_request_id(
            supplied.decode("ascii", errors="ignore") if supplied is not None else None
        )
        payload = json.dumps(
            {"error": {"code": code, "message": message, "request_id": request_id}},
            separators=(",", ":"),
        ).encode()
        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
            *(extra_headers or []),
        ]
        await send(
            {"type": "http.response.start", "status": status_code, "headers": response_headers}
        )
        await send({"type": "http.response.body", "body": payload})


__all__ = ["RequestLimitsMiddleware"]
