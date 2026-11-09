from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.cache import RedisMaterializationCache
from scat.services.materialization_service import MaterializationService


class FakeRedisClient:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.items.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        self.items[key] = value
        self.expirations[key] = ex
        return True


class FakeRuntime:
    def __init__(self, client: Any, *, enabled: bool = True) -> None:
        self.client = client
        self.enabled = enabled
        self.failures: list[BaseException] = []

    async def get_client(self) -> Any:
        return self.client if self.enabled else None

    async def mark_failed(self, error: BaseException) -> None:
        self.failures.append(error)


def _cache(runtime: FakeRuntime, *, max_payload_bytes: int = 100_000) -> RedisMaterializationCache:
    return RedisMaterializationCache(
        runtime,  # type: ignore[arg-type]
        key_prefix="test",
        ttl_seconds=30,
        max_payload_bytes=max_payload_bytes,
    )


def _document(**overrides: Any) -> dict[str, Any]:
    value = {
        "batch": "B",
        "external_id": "1",
        "source_version": "source-v1",
        "algorithm_version": "algorithm-v1",
        "generated_at": datetime(2026, 9, 28, tzinfo=UTC),
        "point_count": 10,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_shared_cache_round_trip_is_version_and_business_key_isolated() -> None:
    client = FakeRedisClient()
    cache = _cache(FakeRuntime(client))
    business_key = {"batch": "B", "external_id": "1"}

    assert await cache.put(
        "flight_features",
        _document(),
        business_key=business_key,
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    )
    assert len(client.items) == 1
    stored_key = next(iter(client.items))
    assert stored_key.startswith("test:materialization:flight_features:")
    assert client.expirations[stored_key] == 30

    cached = await cache.get(
        "flight_features",
        business_key=business_key,
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    )
    assert cached == _document()
    assert (
        await cache.get(
            "flight_features",
            business_key=business_key,
            source_version="source-v1",
            algorithm_version="algorithm-v2",
        )
        is None
    )
    assert cache.key(
        "flight_features",
        business_key=business_key,
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    ) != cache.key(
        "flight_features",
        business_key={"batch": "B", "external_id": "2"},
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    )


@pytest.mark.asyncio
async def test_shared_cache_rejects_unversioned_mismatched_and_oversized_documents() -> None:
    client = FakeRedisClient()
    cache = _cache(FakeRuntime(client), max_payload_bytes=1024)
    identity = {
        "business_key": {"batch": "B", "external_id": "1"},
        "source_version": "source-v1",
        "algorithm_version": "algorithm-v1",
    }

    assert not await cache.put("flight_features", {"batch": "B"}, **identity)
    assert not await cache.put(
        "flight_features",
        _document(algorithm_version="other"),
        **identity,
    )
    assert not await cache.put(
        "flight_features",
        _document(blob="x" * 2000),
        **identity,
    )
    assert client.items == {}


@pytest.mark.asyncio
async def test_service_does_not_cache_an_expired_materialized_record() -> None:
    client = FakeRedisClient()
    cache = _cache(FakeRuntime(client))
    repository = Mock()
    repository.find_latest = AsyncMock(
        return_value=_document(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    service = MaterializationService(
        repository=repository,
        flight_service=Mock(),
        shared_cache=cache,
    )

    assert (
        await service.get_flight_features(
            batch="B",
            external_id="1",
            source_version="source-v1",
            algorithm_version="algorithm-v1",
        )
        is None
    )
    assert client.items == {}


@pytest.mark.asyncio
async def test_shared_cache_failure_is_a_safe_miss() -> None:
    class FailingClient:
        async def get(self, _key: str) -> str | None:
            raise TimeoutError("offline")

    runtime = FakeRuntime(FailingClient())
    cache = _cache(runtime)
    result = await cache.get(
        "batch_statistics",
        business_key={"batch": "B"},
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    )

    assert result is None
    assert len(runtime.failures) == 1


@pytest.mark.asyncio
async def test_two_services_share_cache_and_miss_on_version_change() -> None:
    client = FakeRedisClient()
    first_cache = _cache(FakeRuntime(client))
    second_cache = _cache(FakeRuntime(client))
    document = _document()
    first_repository = Mock()
    first_repository.find_latest = AsyncMock(return_value=document)
    second_repository = Mock()
    second_repository.find_latest = AsyncMock(return_value=None)

    first = MaterializationService(
        repository=first_repository,
        flight_service=Mock(),
        shared_cache=first_cache,
    )
    second = MaterializationService(
        repository=second_repository,
        flight_service=Mock(),
        shared_cache=second_cache,
    )

    assert await first.get_flight_features(
        batch="B",
        external_id="1",
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    ) == document
    assert await second.get_flight_features(
        batch="B",
        external_id="1",
        source_version="source-v1",
        algorithm_version="algorithm-v1",
    ) == document
    second_repository.find_latest.assert_not_awaited()

    assert (
        await second.get_flight_features(
            batch="B",
            external_id="1",
            algorithm_version="algorithm-v2",
        )
        is None
    )
    second_repository.find_latest.assert_awaited_once()
