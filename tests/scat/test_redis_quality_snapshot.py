from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.preprocessing.gbar_v35 import SCAT_GBAR_QUALITY_POLICY_VERSION
from scat.quality_cache import RedisQualitySnapshotCache
from scat.services.flight_service import ScatFlightService


class FakeRedisClient:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.items.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        assert ex == 300
        self.items[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.items.pop(key, None) is not None)


class FakeRuntime:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.enabled = True
        self.failures: list[BaseException] = []

    async def get_client(self) -> Any:
        return self.client

    async def mark_failed(self, error: BaseException) -> None:
        self.failures.append(error)


def _cache(client: Any, *, max_payload_bytes: int = 100_000) -> RedisQualitySnapshotCache:
    return RedisQualitySnapshotCache(
        FakeRuntime(client),  # type: ignore[arg-type]
        key_prefix="test",
        generation_version="quality-v1",
        ttl_seconds=300,
        max_payload_bytes=max_payload_bytes,
    )


def _snapshot(*, batch: str | None = None, external_id: str = "1") -> dict[str, Any]:
    return {
        "scope": "all_scat_flights" if batch is None else "batch",
        "batch": batch,
        "summary": {"flight_count": 1},
        "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
        "_quality_flight_rows": [
            {
                "batch": batch or "B",
                "external_id": external_id,
                "point_count": 10,
                "missing_point_count": 0,
                "outlier_point_count": 0,
                "spike_value_count": 0,
            }
        ],
    }


@pytest.mark.asyncio
async def test_quality_snapshot_round_trip_is_policy_batch_and_generation_bound() -> None:
    client = FakeRedisClient()
    cache = _cache(client)
    snapshot = _snapshot(batch="B")

    assert await cache.put(
        snapshot,
        batch="B",
        policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
    )
    assert await cache.get(
        batch="B",
        policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
    ) == snapshot
    assert (
        await cache.get(
            batch=None,
            policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
        )
        is None
    )
    other_generation = RedisQualitySnapshotCache(
        FakeRuntime(client),  # type: ignore[arg-type]
        key_prefix="test",
        generation_version="quality-v2",
        ttl_seconds=300,
        max_payload_bytes=100_000,
    )
    assert (
        await other_generation.get(
            batch="B",
            policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
        )
        is None
    )


@pytest.mark.asyncio
async def test_quality_snapshot_rejects_full_trajectory_or_oversized_payload() -> None:
    client = FakeRedisClient()
    cache = _cache(client, max_payload_bytes=1024)
    with_trajectory = _snapshot()
    with_trajectory["_quality_flight_rows"][0]["plots"] = [{"lat": 1.0}]

    assert not await cache.put(
        with_trajectory,
        batch=None,
        policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
    )
    assert not await cache.put(
        _snapshot(external_id="x" * 2000),
        batch=None,
        policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
    )
    assert client.items == {}


@pytest.mark.asyncio
async def test_two_services_share_snapshot_and_explicit_rebuild_replaces_it() -> None:
    client = FakeRedisClient()
    first_repository = Mock()
    first_repository.get_model_data_quality_summary = AsyncMock(return_value=_snapshot())
    second_repository = Mock()
    second_repository.get_model_data_quality_summary = AsyncMock(return_value=_snapshot(external_id="2"))
    first = ScatFlightService(repository=first_repository, quality_snapshot_cache=_cache(client))
    second = ScatFlightService(repository=second_repository, quality_snapshot_cache=_cache(client))

    await first.get_model_data_quality_summary()
    shared = await second.list_model_data_quality_clean_flights()
    assert shared["data"]["items"][0]["external_id"] == "1"
    second_repository.get_model_data_quality_summary.assert_not_awaited()

    rebuilt = await second.rebuild_quality_snapshot()
    assert rebuilt["_quality_flight_rows"][0]["external_id"] == "2"
    second_repository.get_model_data_quality_summary.assert_awaited_once_with(
        batch=None,
        include_flight_rows=True,
    )


@pytest.mark.asyncio
async def test_quality_snapshot_failure_falls_back_to_repository() -> None:
    class FailingClient:
        async def get(self, _key: str) -> str | None:
            raise TimeoutError("offline")

    repository = Mock()
    repository.get_model_data_quality_summary = AsyncMock(return_value=_snapshot())
    service = ScatFlightService(
        repository=repository,
        quality_snapshot_cache=_cache(FailingClient()),
    )

    result = await service.get_model_data_quality_summary()
    assert result["data"]["summary"]["flight_count"] == 1
    repository.get_model_data_quality_summary.assert_awaited_once()
