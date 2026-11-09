from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.cache import VersionedMaterializationCache
from scat.materialization.contracts import PredictionMetricsRecord
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import _materialize_batch
from scat.performance import latency_summary, summarize_explain
from scat.services.materialization_service import MaterializationService


def test_versioned_cache_expires_and_evicts_lru() -> None:
    now = [0.0]
    cache = VersionedMaterializationCache(ttl_seconds=5.0, max_entries=2, clock=lambda: now[0])
    cache.put(("a", "v1"), {"value": 1})
    cache.put(("b", "v1"), {"value": 2})
    assert cache.get(("a", "v1")) == {"value": 1}
    cache.put(("c", "v1"), {"value": 3})
    assert cache.get(("b", "v1")) is None
    assert cache.get(("a", "v2")) is None
    now[0] = 5.0
    assert cache.get(("a", "v1")) is None


@pytest.mark.asyncio
async def test_service_cache_is_explicitly_separated_by_versions() -> None:
    repository = Mock()
    repository.find_latest = AsyncMock(
        side_effect=lambda kind, key: {
            "batch": key["batch"],
            "source_version": key["source_version"],
            "algorithm_version": key["algorithm_version"],
            "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    service = MaterializationService(
        repository=repository,
        flight_service=Mock(),
        cache=VersionedMaterializationCache(ttl_seconds=30.0, max_entries=8),
    )

    await service.get_batch_statistics(batch="B", algorithm_version="v1")
    await service.get_batch_statistics(batch="B", algorithm_version="v1")
    await service.get_batch_statistics(batch="B", algorithm_version="v2")

    assert repository.find_latest.await_count == 2


@pytest.mark.asyncio
async def test_bulk_upsert_is_unordered_and_version_keyed() -> None:
    collection = Mock()
    collection.bulk_write = AsyncMock()
    repository = MaterializationRepository(Mock())
    repository.collection = Mock(return_value=collection)  # type: ignore[method-assign]
    record = PredictionMetricsRecord(
        source_version="source-v1",
        algorithm_version="algorithm-v1",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        batch="B",
        external_id="1",
        snapshot_time=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_s=60.0,
        target_time=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        is_valid=False,
        exclusion_reason="test",
    )

    assert await repository.upsert_records("prediction_metrics", [record]) == 1
    operations = collection.bulk_write.await_args.args[0]
    assert collection.bulk_write.await_args.kwargs == {"ordered": False}
    assert operations[0]._filter["source_version"] == record.source_version
    assert operations[0]._filter["algorithm_version"] == record.algorithm_version


@pytest.mark.asyncio
async def test_materialization_batch_caps_concurrency_and_preserves_progress_order() -> None:
    active = 0
    maximum_active = 0
    completed: list[str] = []
    progress: list[int] = []

    async def unit(*, external_id: str, **_: object) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.005)
        completed.append(external_id)
        active -= 1

    materialization_repository = Mock()
    materialization_repository.existing_values = AsyncMock(return_value=set())
    result = await _materialize_batch(
        kind="flight_features",
        unit=unit,
        batch="B",
        flight_repository=Mock(),
        materialization_repository=materialization_repository,
        external_ids=[str(index) for index in range(7)],
        concurrency=3,
        on_progress=lambda update: _append_progress(progress, update),
    )

    assert result["succeeded_count"] == 7
    assert maximum_active == 3
    assert completed == [str(index) for index in range(7)]
    assert progress == list(range(1, 8))


async def _append_progress(target: list[int], update: dict[str, object]) -> None:
    target.append(int(update["processed"]))


def test_explain_summary_flags_collection_scan() -> None:
    indexed = summarize_explain(
        "indexed",
        {
            "queryPlanner": {
                "winningPlan": {
                    "stage": "FETCH",
                    "inputStage": {"stage": "IXSCAN", "indexName": "idx"},
                }
            },
            "executionStats": {
                "nReturned": 2,
                "totalDocsExamined": 2,
                "totalKeysExamined": 2,
                "executionTimeMillis": 1,
            },
        },
    )
    scanned = summarize_explain("scan", {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}})
    assert indexed["ok"] is True
    assert indexed["indexes"] == ["idx"]
    assert scanned["ok"] is False
    assert latency_summary([1.0, 2.0, 3.0])["p95_ms"] == pytest.approx(2.9)
