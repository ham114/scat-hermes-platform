"""Optional bounded Redis client used for shared, non-authoritative state."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from redis.asyncio import Redis

from core.settings import settings

logger = logging.getLogger(__name__)


class RedisRuntime:
    """Lazily owns one async Redis connection pool with bounded timeouts."""

    def __init__(
        self,
        *,
        url: str | None,
        connect_timeout_seconds: float = 0.5,
        socket_timeout_seconds: float = 0.5,
    ) -> None:
        self._url = url
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._socket_timeout_seconds = float(socket_timeout_seconds)
        self._client: Redis[Any] | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def get_client(self) -> Redis[Any] | None:
        if not self._url:
            return None
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                self._client = Redis.from_url(
                    self._url,
                    decode_responses=True,
                    socket_connect_timeout=self._connect_timeout_seconds,
                    socket_timeout=self._socket_timeout_seconds,
                    retry_on_timeout=False,
                    health_check_interval=30,
                )
            return self._client

    async def status(self) -> str:
        client = await self.get_client()
        if client is None:
            return "disabled"
        try:
            return "connected" if await client.ping() else "disconnected"
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self.mark_failed(error)
            return "disconnected"

    async def mark_failed(self, error: BaseException) -> None:
        """Discard a failed pool without logging endpoint or command details."""
        logger.warning("Optional Redis operation failed failure_type=%s", type(error).__name__)
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best-effort pool cleanup
                logger.debug("Failed to close optional Redis pool", exc_info=True)

    async def close(self) -> None:
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()


redis_runtime = RedisRuntime(
    url=settings.REDIS_URL.get_secret_value() if settings.REDIS_URL is not None else None,
    connect_timeout_seconds=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
    socket_timeout_seconds=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
)

__all__ = ["RedisRuntime", "redis_runtime"]
