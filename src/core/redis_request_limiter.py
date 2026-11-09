"""Fail-open Redis global HTTP concurrency and fixed-window rate limits."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

from core.redis_runtime import RedisRuntime, redis_runtime
from core.settings import settings

_ACQUIRE_SCRIPT = """
-- request_acquire_v1
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  return {-1, 0}
end
local count = redis.call('INCR', KEYS[2])
if count == 1 then redis.call('EXPIRE', KEYS[2], tonumber(ARGV[5])) end
if count > tonumber(ARGV[4]) then
  return {-2, count}
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[6])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[7]))
return {1, count}
"""

_RENEW_SCRIPT = """
-- request_renew_v1
if redis.call('ZSCORE', KEYS[1], ARGV[1]) == false then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""

_RELEASE_SCRIPT = """
-- request_release_v1
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


@dataclass(frozen=True, slots=True)
class RedisLimitDecision:
    allowed: bool
    reason: str | None = None
    retry_after_seconds: int = 1
    token: str | None = None


class RedisRequestLimiter:
    """Coordinates process-independent request limits without becoming critical."""

    def __init__(
        self,
        runtime: RedisRuntime,
        *,
        key_prefix: str,
        max_concurrency: int,
        rate_limit: int,
        rate_window_seconds: int,
        slot_ttl_seconds: int,
    ) -> None:
        if min(max_concurrency, rate_limit, rate_window_seconds, slot_ttl_seconds) < 1:
            raise ValueError("Redis request limits must be positive")
        self._runtime = runtime
        self._active_key = f"{key_prefix}:http_limit:active"
        self._rate_prefix = f"{key_prefix}:http_limit:rate"
        self.max_concurrency = int(max_concurrency)
        self.rate_limit = int(rate_limit)
        self.rate_window_seconds = int(rate_window_seconds)
        self.slot_ttl_seconds = int(slot_ttl_seconds)

    @property
    def enabled(self) -> bool:
        return self._runtime.enabled

    @property
    def renewal_interval_seconds(self) -> float:
        return max(1.0, self.slot_ttl_seconds / 3.0)

    async def acquire(self) -> RedisLimitDecision:
        client = await self._runtime.get_client()
        if client is None:
            return RedisLimitDecision(allowed=True)
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + self.slot_ttl_seconds * 1000
        window = now_ms // (self.rate_window_seconds * 1000)
        retry_after = self.rate_window_seconds - int(
            (now_ms // 1000) % self.rate_window_seconds
        )
        token = uuid4().hex
        try:
            raw = await client.eval(
                _ACQUIRE_SCRIPT,
                2,
                self._active_key,
                f"{self._rate_prefix}:{window}",
                now_ms,
                expires_ms,
                self.max_concurrency,
                self.rate_limit,
                self.rate_window_seconds + 1,
                token,
                self.slot_ttl_seconds + 1,
            )
            code = int(raw[0]) if isinstance(raw, list) and raw else 0
            if code == 1:
                return RedisLimitDecision(allowed=True, token=token)
            if code == -1:
                return RedisLimitDecision(allowed=False, reason="concurrency")
            if code == -2:
                return RedisLimitDecision(
                    allowed=False,
                    reason="rate",
                    retry_after_seconds=max(1, retry_after),
                )
            return RedisLimitDecision(allowed=True)
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return RedisLimitDecision(allowed=True)

    async def renew(self, token: str) -> bool:
        client = await self._runtime.get_client()
        if client is None:
            return False
        try:
            renewed = await client.eval(
                _RENEW_SCRIPT,
                1,
                self._active_key,
                token,
                int(time.time() * 1000) + self.slot_ttl_seconds * 1000,
                self.slot_ttl_seconds + 1,
            )
            return int(renewed or 0) == 1
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return False

    async def release(self, token: str) -> None:
        client = await self._runtime.get_client()
        if client is None:
            return
        try:
            await client.eval(_RELEASE_SCRIPT, 1, self._active_key, token)
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)


redis_request_limiter = RedisRequestLimiter(
    redis_runtime,
    key_prefix=settings.REDIS_KEY_PREFIX,
    max_concurrency=settings.REDIS_HTTP_MAX_CONCURRENCY,
    rate_limit=settings.REDIS_HTTP_RATE_LIMIT,
    rate_window_seconds=settings.REDIS_HTTP_RATE_WINDOW_SECONDS,
    slot_ttl_seconds=settings.REDIS_HTTP_SLOT_TTL_SECONDS,
)


__all__ = ["RedisLimitDecision", "RedisRequestLimiter", "redis_request_limiter"]
