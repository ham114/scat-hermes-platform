"""M4-04 批次统计物化任务测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.contracts import BatchStatisticsRecord
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import materialize_batch_statistics
from src.scat.repositories.flight_repository import ScatFlightRepository

BATCH = "scat20161015_20161021"

STATISTICS = {
    "batch": BATCH,
    "flight_count": 12,
    "unique_callsign_count": 11,
    "aircraft_type_count": 3,
    "departure_airport_count": 2,
    "arrival_airport_count": 4,
    "total_track_points": 2400,
    "average_track_points": 200.0,
    "minimum_track_points": 150,
    "maximum_track_points": 260,
    "flights_with_prediction": 8,
    "total_prediction_snapshots": 30,
}

QUALITY = {
    "missing_callsign_count": 0,
    "missing_aircraft_type_count": 1,
    "missing_departure_airport_count": 0,
    "missing_arrival_airport_count": 0,
    "missing_iobt_count": 2,
    "empty_trajectory_count": 1,
    "flights_without_prediction_count": 4,
}


def make_flight_repository() -> ScatFlightRepository:
    repo = Mock(spec=ScatFlightRepository)
    repo.get_batch_statistics = AsyncMock(return_value=STATISTICS)
    repo.get_batch_data_quality = AsyncMock(return_value=QUALITY)
    return repo


@pytest.fixture
def upsert_calls() -> list[tuple[dict, dict]]:
    calls: list[tuple[dict, dict]] = []
    collection = Mock()
    collection.replace_one = AsyncMock(side_effect=lambda f, d, **kw: calls.append((f, d)))
    database = Mock()
    database.__getitem__ = Mock(return_value=collection)
    return calls


@pytest.mark.asyncio
async def test_materialize_batch_statistics_upserts_once(upsert_calls):
    repo = MaterializationRepository(Mock())
    repo.collection = Mock(return_value=_collection(upsert_calls))  # type: ignore[method-assign]

    generated = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    record = await materialize_batch_statistics(
        batch=BATCH,
        flight_repository=make_flight_repository(),
        materialization_repository=repo,
        generated_at=generated,
    )

    assert isinstance(record, BatchStatisticsRecord)
    assert record.batch == BATCH
    assert record.flight_count == 12
    assert record.missing_iobt_count == 2
    assert len(upsert_calls) == 1

    filter_doc, document = upsert_calls[0]
    # 唯一键 = 业务键 + 版本键（幂等与版本隔离）
    assert filter_doc == {
        "batch": BATCH,
        "source_version": "scat-import-2",
        "algorithm_version": "1",
    }
    assert document["batch"] == BATCH
    assert document["generated_at"] == "2026-09-17T12:00:00Z"


def _collection(calls):
    collection = Mock()
    collection.replace_one = AsyncMock(side_effect=lambda f, d, **kw: calls.append((f, d)))
    return collection


@pytest.mark.asyncio
async def test_materialize_batch_statistics_version_isolation(upsert_calls):
    repo = MaterializationRepository(Mock())
    repo.collection = Mock(return_value=_collection(upsert_calls))  # type: ignore[method-assign]

    await materialize_batch_statistics(
        batch=BATCH,
        flight_repository=make_flight_repository(),
        materialization_repository=repo,
        algorithm_version="2",
    )
    await materialize_batch_statistics(
        batch=BATCH,
        flight_repository=make_flight_repository(),
        materialization_repository=repo,
        algorithm_version="1",
    )

    assert len(upsert_calls) == 2
    # 不同算法版本生成不同唯一键，旧版本不被覆盖
    assert upsert_calls[0][0]["algorithm_version"] == "2"
    assert upsert_calls[1][0]["algorithm_version"] == "1"


@pytest.mark.asyncio
async def test_materialize_batch_statistics_rejects_empty_batch():
    flight_repository = make_flight_repository()
    flight_repository.get_batch_statistics = AsyncMock(return_value=None)  # type: ignore[method-assign]
    repo = MaterializationRepository(Mock())
    repo.collection = Mock(return_value=_collection([]))  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="批次不存在或无数据"):
        await materialize_batch_statistics(
            batch="no-such-batch",
            flight_repository=flight_repository,
            materialization_repository=repo,
        )
