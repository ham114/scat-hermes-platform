"""M4-09 物化读取服务测试：有效物化优先、回退策略、任务查询与总览接线。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.tasks import (
    BATCH_STATISTICS_ALGORITHM_VERSION,
    DEFAULT_SOURCE_VERSION,
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
)
from scat.services.flight_service import ScatFlightService
from scat.services.materialization_service import (
    JOB_LIST_LIMIT_MAX,
    MaterializationService,
)

BATCH = "scat20161015_20161021"
GENERATED = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

STATISTICS: dict[str, Any] = {
    "batch": BATCH,
    "flight_count": 13138,
    "unique_callsign_count": 12000,
    "aircraft_type_count": 25,
    "departure_airport_count": 200,
    "arrival_airport_count": 210,
    "total_track_points": 5500081,
    "average_track_points": 418.6,
    "minimum_track_points": 2,
    "maximum_track_points": 9000,
    "flights_with_prediction": 13136,
    "total_prediction_snapshots": 200000,
}

QUALITY: dict[str, Any] = {
    "missing_callsign_count": 0,
    "missing_aircraft_type_count": 5,
    "missing_departure_airport_count": 0,
    "missing_arrival_airport_count": 0,
    "missing_iobt_count": 12,
    "empty_trajectory_count": 3,
    "flights_without_prediction_count": 2,
}


def _record(**overrides: Any) -> dict[str, Any]:
    document = {
        **STATISTICS,
        **QUALITY,
        "source_version": DEFAULT_SOURCE_VERSION,
        "algorithm_version": BATCH_STATISTICS_ALGORITHM_VERSION,
        "generated_at": GENERATED,
        "expires_at": None,
    }
    document.update(overrides)
    return document


class FakeJobCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = list(documents)

    def sort(self, key: str, direction: int) -> FakeJobCursor:
        self._documents.sort(key=lambda item: item.get(key), reverse=direction < 0)
        return self

    def limit(self, count: int) -> FakeJobCursor:
        self._documents = self._documents[:count]
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return list(self._documents)


class FakeJobCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents
        self.queries: list[dict[str, Any]] = []

    def find(
        self, filters: dict[str, Any], projection: dict[str, Any] | None = None
    ) -> FakeJobCursor:
        self.queries.append(filters)
        matched = [
            document
            for document in self.documents
            if all(document.get(key) == value for key, value in filters.items())
        ]
        return FakeJobCursor(matched)


class FakeMaterializationRepository:
    def __init__(
        self,
        *,
        latest: dict[str, Any] | None = None,
        jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        self._latest = latest
        self.job_collection = FakeJobCollection(jobs or [])
        self.lookups: list[tuple[str, dict[str, Any]]] = []

    async def find_latest(self, kind: str, business_key: dict[str, Any]) -> dict[str, Any] | None:
        self.lookups.append((kind, business_key))
        return self._latest

    async def find_job(self, job_id: str) -> dict[str, Any] | None:
        for document in self.job_collection.documents:
            if document.get("job_id") == job_id:
                return document
        return None

    def collection(self, kind: str) -> FakeJobCollection:
        assert kind == "jobs"
        return self.job_collection


def _flight_repository(*, statistics: dict[str, Any] | None) -> Any:
    repository = Mock()
    repository.get_batch_statistics = AsyncMock(return_value=statistics)
    repository.get_batch_data_quality = AsyncMock(return_value=QUALITY)
    repository.get_aircraft_type_distribution = AsyncMock(
        return_value=[{"aircraft_type": "B738", "flight_count": 3}]
    )
    repository.get_route_distribution = AsyncMock(return_value=[])
    repository.get_airport_distributions = AsyncMock(
        return_value={"departures": [], "arrivals": []}
    )
    repository.get_flight_volume = AsyncMock(return_value=[])
    return repository


def _service(
    *,
    latest: dict[str, Any] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    statistics: dict[str, Any] | None = None,
) -> tuple[MaterializationService, FakeMaterializationRepository, Any]:
    repository = FakeMaterializationRepository(latest=latest, jobs=jobs)
    flight_repository = _flight_repository(statistics=statistics)
    flight_service = ScatFlightService(repository=flight_repository)
    service = MaterializationService(repository=repository, flight_service=flight_service)
    return service, repository, flight_repository


@pytest.mark.asyncio
async def test_read_batch_statistics_prefers_valid_materialized_record():
    service, repository, flight_repository = _service(latest=_record())

    result = await service.read_batch_statistics(batch=BATCH)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["source"] == "materialized"
    assert data["statistics"]["flight_count"] == 13138
    assert data["data_quality"]["missing_iobt_count"] == 12
    assert data["generated_at"] == GENERATED
    assert data["fallback_reason"] is None
    # 有效物化结果命中时不再扫描原始集合
    flight_repository.get_batch_statistics.assert_not_awaited()
    flight_repository.get_batch_data_quality.assert_not_awaited()
    kind, business_key = repository.lookups[0]
    assert kind == "batch_statistics"
    assert business_key == {
        "batch": BATCH,
        "source_version": DEFAULT_SOURCE_VERSION,
        "algorithm_version": BATCH_STATISTICS_ALGORITHM_VERSION,
    }


@pytest.mark.asyncio
async def test_read_batch_statistics_falls_back_live_when_record_missing():
    service, _, flight_repository = _service(statistics=STATISTICS)

    result = await service.read_batch_statistics(batch=BATCH)

    data = result["data"]
    assert data["source"] == "live"
    assert data["fallback_reason"] == "missing"
    assert data["statistics"] == STATISTICS
    assert data["generated_at"] is None
    flight_repository.get_batch_statistics.assert_awaited_once_with(batch=BATCH)


@pytest.mark.asyncio
async def test_read_batch_statistics_reports_expired_record():
    expired = _record(expires_at=GENERATED - timedelta(days=1))
    service, _, _ = _service(latest=expired, statistics=STATISTICS)

    result = await service.read_batch_statistics(batch=BATCH, now=GENERATED)

    assert result["data"]["source"] == "live"
    assert result["data"]["fallback_reason"] == "expired"


@pytest.mark.asyncio
async def test_read_batch_statistics_live_mode_skips_materialized_lookup():
    service, repository, _ = _service(latest=_record(), statistics=STATISTICS)

    result = await service.read_batch_statistics(batch=BATCH, statistics_source="live")

    assert result["data"]["source"] == "live"
    assert result["data"]["fallback_reason"] is None
    assert repository.lookups == []


@pytest.mark.asyncio
async def test_read_batch_statistics_reports_unknown_batch_as_not_found():
    service, _, _ = _service(latest=None, statistics=None)

    result = await service.read_batch_statistics(batch="no-such-batch")

    assert result["status"] == "not_found"
    assert result["query"] == {"batch": "no-such-batch"}


@pytest.mark.asyncio
async def test_get_batch_statistics_returns_none_for_other_version():
    service, repository, _ = _service(latest=None)

    assert await service.get_batch_statistics(batch=BATCH) is None
    _, business_key = repository.lookups[0]
    # 版本隔离：读取只认当前 source_version + algorithm_version
    assert business_key["algorithm_version"] == BATCH_STATISTICS_ALGORITHM_VERSION
    assert business_key["source_version"] == DEFAULT_SOURCE_VERSION


@pytest.mark.asyncio
async def test_get_flight_features_uses_flight_business_key():
    document = {"batch": BATCH, "external_id": "100002", "distance_km": 720.0}
    service, repository, _ = _service(latest=document)

    assert await service.get_flight_features(batch=BATCH, external_id=100002) == document
    kind, business_key = repository.lookups[0]
    assert kind == "flight_features"
    assert business_key == {
        "batch": BATCH,
        "external_id": "100002",
        "source_version": DEFAULT_SOURCE_VERSION,
        "algorithm_version": FLIGHT_FEATURES_ALGORITHM_VERSION,
    }


@pytest.mark.asyncio
async def test_get_flight_phase_segments_uses_phase_segments_kind():
    document = {"batch": BATCH, "external_id": "100002", "segment_count": 4}
    service, repository, _ = _service(latest=document)

    assert await service.get_flight_phase_segments(batch=BATCH, external_id=100002) == document
    kind, business_key = repository.lookups[0]
    assert kind == "phase_segments"
    assert business_key == {
        "batch": BATCH,
        "external_id": "100002",
        "source_version": DEFAULT_SOURCE_VERSION,
        "algorithm_version": FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    }


def _job(job_id: str, *, kind: str, status: str, generated: datetime) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "kind": kind,
        "batch": BATCH,
        "status": status,
        "progress": 1.0,
        "total_count": 200,
        "processed_count": 200,
        "failed_targets": ["100002"],
        "failure_detail": {"100002": "航班无有效轨迹点"},
        "generated_at": generated,
    }


@pytest.mark.asyncio
async def test_list_jobs_filters_sorts_and_maps_failure_fields():
    jobs = [
        _job(
            "job-old",
            kind="flight_features",
            status="partial",
            generated=GENERATED - timedelta(hours=2),
        ),
        _job("job-new", kind="flight_features", status="succeeded", generated=GENERATED),
        _job("job-stats", kind="batch_statistics", status="succeeded", generated=GENERATED),
    ]
    service, repository, _ = _service(jobs=jobs)

    result = await service.list_jobs(kind="flight_features", limit=5)

    assert result["status"] == "ok"
    assert repository.job_collection.queries == [{"kind": "flight_features"}]
    items = result["data"]["items"]
    assert [item["job_id"] for item in items] == ["job-new", "job-old"]
    assert items[1]["failed_count"] == 1
    assert items[1]["failed_targets"] == ["100002"]
    assert items[1]["failure_detail"] == {"100002": "航班无有效轨迹点"}
    assert result["data"]["limit"] == 5


@pytest.mark.asyncio
async def test_list_jobs_rejects_unknown_filters_and_oversized_limit():
    service, _, _ = _service(jobs=[])

    with pytest.raises(ValueError, match="未知物化任务种类"):
        await service.list_jobs(kind="secrets")
    with pytest.raises(ValueError, match="未知任务状态"):
        await service.list_jobs(status_filter="unknown")
    with pytest.raises(ValueError, match="limit"):
        await service.list_jobs(limit=JOB_LIST_LIMIT_MAX + 1)


@pytest.mark.asyncio
async def test_get_job_returns_not_found_envelope():
    service, _, _ = _service(jobs=[])

    result = await service.get_job("job-missing")

    assert result["status"] == "not_found"
    assert result["query"] == {"job_id": "job-missing"}


@pytest.mark.asyncio
async def test_overview_uses_materialized_statistics_when_available():
    materialization_service = Mock()
    materialization_service.get_batch_statistics = AsyncMock(return_value=_record())
    flight_repository = _flight_repository(statistics=STATISTICS)
    service = ScatFlightService(
        repository=flight_repository,
        materialization_service=materialization_service,
    )

    result = await service.get_batch_overview(batch=BATCH)

    data = result["data"]
    assert data["statistics_source"] == "materialized"
    assert data["statistics"]["flight_count"] == 13138
    assert data["statistics_generated_at"] == "2026-09-17T12:00:00Z"
    assert data["top_aircraft_types"][0]["aircraft_type"] == "B738"
    flight_repository.get_batch_statistics.assert_not_awaited()
    flight_repository.get_batch_data_quality.assert_not_awaited()


@pytest.mark.asyncio
async def test_overview_falls_back_to_live_statistics_without_record():
    materialization_service = Mock()
    materialization_service.get_batch_statistics = AsyncMock(return_value=None)
    flight_repository = _flight_repository(statistics=STATISTICS)
    service = ScatFlightService(
        repository=flight_repository,
        materialization_service=materialization_service,
    )

    result = await service.get_batch_overview(batch=BATCH, statistics_source="auto")

    data = result["data"]
    assert data["statistics_source"] == "live"
    assert data["statistics_generated_at"] is None
    flight_repository.get_batch_statistics.assert_awaited_once_with(batch=BATCH)
