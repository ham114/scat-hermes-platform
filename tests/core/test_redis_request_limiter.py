from __future__ import annotations

from typing import Any

import pytest

from core.redis_request_limiter import RedisRequestLimiter


class FakeRedisClient:
    def __init__(self) -> None:
        self.active: dict[str, int] = {}
        self.rates: dict[str, int] = {}

    async def eval(self, script: str, _count: int, *args: Any) -> Any:
        if "request_acquire_v1" in script:
            active_key, rate_key, now_ms, expires_ms, maximum, rate, _rate_ttl, token, _ttl = args
            del active_key
            self.active = {
                key: expiry for key, expiry in self.active.items() if expiry > int(now_ms)
            }
            if len(self.active) >= int(maximum):
                return [-1, 0]
            count = self.rates.get(str(rate_key), 0) + 1
            self.rates[str(rate_key)] = count
            if count > int(rate):
                return [-2, count]
            self.active[str(token)] = int(expires_ms)
            return [1, count]
        if "request_renew_v1" in script:
            _active_key, token, expires_ms, _ttl = args
            if str(token) not in self.active:
                return 0
            self.active[str(token)] = int(expires_ms)
            return 1
        if "request_release_v1" in script:
            _active_key, token = args
            return int(self.active.pop(str(token), None) is not None)
        raise AssertionError("unknown script")


class FakeRuntime:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.enabled = True
        self.failures: list[BaseException] = []

    async def get_client(self) -> Any:
        return self.client

    async def mark_failed(self, error: BaseException) -> None:
        self.failures.append(error)


def _limiter(client: Any, *, prefix: str = "test", rate: int = 10) -> RedisRequestLimiter:
    return RedisRequestLimiter(
        FakeRuntime(client),  # type: ignore[arg-type]
        key_prefix=prefix,
        max_concurrency=1,
        rate_limit=rate,
        rate_window_seconds=60,
        slot_ttl_seconds=30,
    )


@pytest.mark.asyncio
async def test_global_concurrency_is_shared_and_release_recovers_slot() -> None:
    client = FakeRedisClient()
    first = _limiter(client)
    second = _limiter(client)

    acquired = await first.acquire()
    rejected = await second.acquire()
    assert acquired.allowed and acquired.token is not None
    assert not rejected.allowed and rejected.reason == "concurrency"

    assert await first.renew(acquired.token)
    await first.release(acquired.token)
    recovered = await second.acquire()
    assert recovered.allowed and recovered.token is not None
    await second.release(recovered.token)


@pytest.mark.asyncio
async def test_global_fixed_window_rate_limit_is_shared() -> None:
    client = FakeRedisClient()
    first = _limiter(client, prefix="rate", rate=2)
    second = _limiter(client, prefix="rate", rate=2)

    for limiter in (first, second):
        decision = await limiter.acquire()
        assert decision.allowed and decision.token is not None
        await limiter.release(decision.token)
    rejected = await first.acquire()
    assert not rejected.allowed
    assert rejected.reason == "rate"
    assert rejected.retry_after_seconds >= 1


@pytest.mark.asyncio
async def test_expired_global_slot_is_reclaimed() -> None:
    client = FakeRedisClient()
    limiter = _limiter(client)
    acquired = await limiter.acquire()
    assert acquired.allowed and acquired.token is not None
    client.active[acquired.token] = 0

    replacement = await limiter.acquire()
    assert replacement.allowed and replacement.token is not None
    await limiter.release(replacement.token)


@pytest.mark.asyncio
async def test_redis_limiter_failure_is_fail_open() -> None:
    class FailingClient:
        async def eval(self, *_args: Any) -> Any:
            raise TimeoutError("offline")

    runtime = FakeRuntime(FailingClient())
    limiter = RedisRequestLimiter(
        runtime,  # type: ignore[arg-type]
        key_prefix="test",
        max_concurrency=1,
        rate_limit=1,
        rate_window_seconds=60,
        slot_ttl_seconds=30,
    )
    decision = await limiter.acquire()
    assert decision.allowed and decision.token is None
    assert len(runtime.failures) == 1
