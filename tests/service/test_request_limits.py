from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from core.redis_request_limiter import RedisLimitDecision
from service.request_limits import RequestLimitsMiddleware


def _scope(*, path: str = "/api", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("test", 1),
        "server": ("test", 80),
    }


def _receiver(messages: list[dict[str, Any]]) -> Callable[[], Awaitable[dict[str, Any]]]:
    queue = list(messages)

    async def receive() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return receive


def _sender(target: list[dict[str, Any]]) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def send(message: dict[str, Any]) -> None:
        target.append(message)

    return send


@pytest.mark.asyncio
async def test_rejects_declared_and_streamed_oversized_bodies() -> None:
    called = 0

    async def app(scope: dict, receive: Any, send: Any) -> None:
        nonlocal called
        called += 1

    middleware = RequestLimitsMiddleware(app, max_body_bytes=4, max_concurrency=2)
    declared: list[dict[str, Any]] = []
    await middleware(
        _scope(headers=[(b"content-length", b"5")]),
        _receiver([]),
        _sender(declared),
    )
    streamed: list[dict[str, Any]] = []
    await middleware(
        _scope(),
        _receiver(
            [
                {"type": "http.request", "body": b"abc", "more_body": True},
                {"type": "http.request", "body": b"de", "more_body": False},
            ]
        ),
        _sender(streamed),
    )

    assert declared[0]["status"] == 413
    assert streamed[0]["status"] == 413
    assert called == 0


@pytest.mark.asyncio
async def test_rejects_excess_concurrency_and_releases_slot() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestLimitsMiddleware(app, max_body_bytes=10, max_concurrency=1)
    first: list[dict[str, Any]] = []
    first_task = asyncio.create_task(
        middleware(
            _scope(),
            _receiver([{"type": "http.request", "body": b"", "more_body": False}]),
            _sender(first),
        )
    )
    await entered.wait()

    second: list[dict[str, Any]] = []
    await middleware(
        _scope(),
        _receiver([{"type": "http.request", "body": b"", "more_body": False}]),
        _sender(second),
    )
    assert second[0]["status"] == 429
    assert (b"retry-after", b"1") in second[0]["headers"]

    release.set()
    await first_task
    assert first[0]["status"] == 200


@pytest.mark.asyncio
async def test_global_limit_rejects_business_request_and_health_is_exempt() -> None:
    class RejectingLimiter:
        renewal_interval_seconds = 1.0

        def __init__(self) -> None:
            self.calls = 0

        async def acquire(self) -> RedisLimitDecision:
            self.calls += 1
            return RedisLimitDecision(False, reason="rate", retry_after_seconds=17)

        async def renew(self, _token: str) -> bool:
            return False

        async def release(self, _token: str) -> None:
            return None

    called = 0

    async def app(scope: dict, receive: Any, send: Any) -> None:
        nonlocal called
        called += 1

    limiter = RejectingLimiter()
    middleware = RequestLimitsMiddleware(
        app,
        max_body_bytes=10,
        max_concurrency=2,
        global_limiter=limiter,  # type: ignore[arg-type]
    )
    rejected: list[dict[str, Any]] = []
    await middleware(
        _scope(),
        _receiver([{"type": "http.request", "body": b"", "more_body": False}]),
        _sender(rejected),
    )
    assert rejected[0]["status"] == 429
    assert (b"retry-after", b"17") in rejected[0]["headers"]

    await middleware(_scope(path="/health/ready"), _receiver([]), _sender([]))
    assert limiter.calls == 1
    assert called == 1
