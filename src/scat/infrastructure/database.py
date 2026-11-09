import asyncio
from contextlib import suppress
from typing import Any

from pymongo import AsyncMongoClient

from core.observability import MongoCommandObserver
from core.settings import settings


class FlightDatabase:
    """SCAT业务MongoDB连接管理器。"""

    def __init__(self) -> None:
        self._client: AsyncMongoClient | None = None
        self._analytics_client: AsyncMongoClient | None = None
        self._connect_lock = asyncio.Lock()

    @staticmethod
    def _event_listeners() -> list[MongoCommandObserver]:
        return [
            MongoCommandObserver(
                slow_ms=float(getattr(settings, "OBSERVABILITY_SLOW_MONGO_MS", 1000.0))
            )
        ]

    async def connect(self) -> None:
        if self._client is not None:
            return

        async with self._connect_lock:
            # Another coroutine may have completed the connection while this
            # coroutine was waiting for the lock.
            if self._client is not None:
                return

            uri = settings.FLIGHT_MONGO_URI.get_secret_value()
            candidate: AsyncMongoClient[Any] = AsyncMongoClient(
                uri,
                connectTimeoutMS=settings.FLIGHT_MONGO_CONNECT_TIMEOUT_MS,
                serverSelectionTimeoutMS=settings.FLIGHT_MONGO_SERVER_SELECTION_TIMEOUT_MS,
                socketTimeoutMS=settings.FLIGHT_MONGO_SOCKET_TIMEOUT_MS,
                timeoutMS=settings.FLIGHT_MONGO_OPERATION_TIMEOUT_MS,
                retryReads=True,
                retryWrites=True,
                event_listeners=self._event_listeners(),
            )

            try:
                await candidate.admin.command("ping")
            except BaseException:
                # Do not retain a client that never established a usable
                # connection. A later request can then retry after MongoDB
                # becomes available again.
                with suppress(Exception):
                    await candidate.close()
                raise

            self._client = candidate

    async def ping(self) -> None:
        """Verify that MongoDB is currently reachable."""
        await self.connect()
        await self.client.admin.command("ping")

    @property
    def client(self) -> AsyncMongoClient:
        if self._client is None:
            raise RuntimeError("Flight MongoDB has not been initialized.")

        return self._client

    @property
    def db(self) -> Any:
        return self.client[settings.FLIGHT_MONGO_DB]

    def get_collection(self, collection_name: str) -> Any:
        return self.db[collection_name]

    def get_analytics_collection(self, collection_name: str) -> Any:
        """长耗时分析聚合专用集合句柄。

        独立于常规客户端：使用略高于服务端180秒maxTimeMS的210秒
        客户端上限，既允许合法长查询完成，也避免网络故障时无限等待。
        """
        if self._analytics_client is None:
            uri = settings.FLIGHT_MONGO_URI.get_secret_value()
            self._analytics_client = AsyncMongoClient(
                uri,
                connectTimeoutMS=settings.FLIGHT_MONGO_CONNECT_TIMEOUT_MS,
                serverSelectionTimeoutMS=settings.FLIGHT_MONGO_SERVER_SELECTION_TIMEOUT_MS,
                socketTimeoutMS=210_000,
                timeoutMS=210_000,
                retryReads=True,
                retryWrites=True,
                event_listeners=self._event_listeners(),
            )
        return self._analytics_client[settings.FLIGHT_MONGO_DB][collection_name]

    async def close(self) -> None:
        async with self._connect_lock:
            clients = [self._client, self._analytics_client]
            self._client = None
            self._analytics_client = None
            for client in clients:
                if client is not None:
                    await client.close()


flight_database = FlightDatabase()
