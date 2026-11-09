"""M4-06/M4-07 物化任务编排测试：任务记录、进度、失败隔离与重试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.contracts import MaterializationJobRecord
from scat.materialization.jobs import (
    MaterializationJobOutcome,
    failed_targets_of,
    new_job_id,
    run_batch_statistics_job,
    run_flight_features_job,
    run_flight_phase_segments_job,
)
from scat.materialization.tasks import FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION

BATCH = "scat20161015_20161021"
GENERATED = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

BASIS: dict[str, Any] = {
    "aircraft_type": "B738",
    "point_count": 120,
    "duration_s": 900.0,
    "distance_m": 180_000.0,
}

PHASES: list[dict[str, Any]] = [
    {
        "_id": 0,
        "point_count": 120,
        "start_time": datetime(2016, 10, 16, 16, 0, tzinfo=UTC),
        "end_time": datetime(2016, 10, 16, 16, 15, tzinfo=UTC),
        "min_flight_level": 3000.0,
        "max_flight_level": 3100.0,
        "average_ground_speed_mps": 200.0,
        "maximum_ground_speed_mps": 220.0,
    }
]


class FakeMaterializationRepository:
    """只实现任务编排用到的接口，记录任务文档与物化记录。"""

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        jobs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.existing = set(existing or ())
        self.job_documents: dict[str, dict[str, Any]] = dict(jobs or {})
        self.records: list[tuple[str, str]] = []
        self.job_history: list[dict[str, Any]] = []

    async def existing_values(
        self,
        kind: str,
        field: str,
        business_key: dict[str, Any],
    ) -> set[str]:
        return set(self.existing)

    async def upsert_record(self, kind: str, record: Any) -> None:
        self.records.append((kind, record.external_id if hasattr(record, "external_id") else ""))

    async def upsert_job(self, job: MaterializationJobRecord) -> None:
        document = job.model_dump(mode="json")
        self.job_documents[job.job_id] = document
        self.job_history.append(document)

    async def find_job(self, job_id: str) -> dict[str, Any] | None:
        return self.job_documents.get(job_id)


# M4-11 阶段秒数输入：两行平飞（单段）——作业生命周期测试只关心任务编排，不关注阶段秒数。
DEFAULT_PHASE_POINTS: list[dict[str, Any]] = [
    {
        "timestamp": "2016-10-16T16:00:00",
        "lat": 59.0,
        "lon": 18.0,
        "measured_flight_level": 350.0,
        "vertical_mode": 0,
    },
    {
        "timestamp": "2016-10-16T16:01:00",
        "lat": 59.01,
        "lon": 18.0,
        "measured_flight_level": 350.0,
        "vertical_mode": 0,
    },
]


class FakeFlightRepository:
    """按页面返回航班 external_id，并按航班返回特征基础聚合。"""

    def __init__(
        self,
        *,
        pages: list[list[str]] | None = None,
        basis_by_id: dict[str, dict[str, Any]] | None = None,
        phases: list[dict[str, Any]] | None = None,
        points: list[dict[str, Any]] | None = None,
        batch_total: int | None = None,
    ) -> None:
        self._pages = list(pages or [])
        self._basis_by_id = dict(basis_by_id or {})
        self._phases = PHASES if phases is None else phases
        self._points = DEFAULT_PHASE_POINTS if points is None else points
        self._batch_total = batch_total if batch_total is not None else self.count_pages()
        self.collection = Mock()
        self.collection.find = Mock(return_value=self._find_cursor())
        self.collection.aggregate = AsyncMock(side_effect=self._aggregate)

    def count_pages(self) -> int:
        return sum(len(page) for page in self._pages)

    def _find_cursor(self) -> Mock:
        """按页返回文档形式的 external_id（模拟 _id 游标分页）。"""
        cursor = Mock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(side_effect=[*self._document_pages(), []])
        return cursor

    def _document_pages(self) -> list[list[dict[str, Any]]]:
        pages: list[list[dict[str, Any]]] = []
        cursor_id = 0
        for page in self._pages:
            documents = []
            for external_id in page:
                cursor_id += 1
                documents.append({"_id": cursor_id, "source": {"external_id": external_id}})
            pages.append(documents)
        return pages

    def _aggregate(self, pipeline: list[dict[str, Any]]) -> Mock:
        external_id = str(pipeline[0]["$match"]["source.external_id"])
        cursor = Mock()
        cursor.to_list = AsyncMock(
            return_value=[self._basis_by_id[external_id]]
            if external_id in self._basis_by_id
            else []
        )
        return cursor

    async def get_trajectory_phase_stats(self, *, batch: str, external_id: Any) -> Any:
        return self._phases

    async def get_trajectory_points_for_phases(
        self, *, batch: str, external_id: Any, limit: int = 20000
    ) -> list[dict[str, Any]]:
        return self._points

    async def count_all(self, *, batch: str | None = None) -> int:
        return self._batch_total


@pytest.mark.asyncio
async def test_run_flight_features_job_records_lifecycle_and_progress():
    repository = FakeMaterializationRepository()
    flight_repository = FakeFlightRepository(
        pages=[["100001", "100002", "100003"]],
        basis_by_id={"100001": BASIS, "100002": BASIS, "100003": BASIS},
    )
    seen: list[MaterializationJobRecord] = []

    async def on_progress(record: MaterializationJobRecord) -> None:
        seen.append(record.model_copy())

    outcome = await run_flight_features_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        job_id="job-1",
        generated_at=GENERATED,
        progress_interval=2,
        on_progress=on_progress,
    )

    assert isinstance(outcome, MaterializationJobOutcome)
    assert outcome.status == "succeeded"
    assert outcome.total_count == 3
    assert outcome.processed_count == 3
    assert outcome.succeeded_count == 3
    assert len(repository.records) == 3

    statuses = [item["status"] for item in repository.job_history]
    assert statuses[0] == "running"
    assert statuses[-1] == "succeeded"
    final = repository.job_documents["job-1"]
    assert final["progress"] == 1.0
    assert final["processed_count"] == 3
    assert final["failed_targets"] == []
    assert final["expires_at"] is not None
    # 进度回调在每次写入后触发：开始、每 2 个单位、结束
    assert [item.status for item in seen][-1] == "succeeded"
    assert max(item.processed_count for item in seen) == 3


@pytest.mark.asyncio
async def test_run_flight_phase_segments_job_records_lifecycle_and_progress():
    """M4-12：段级标注任务复用 M4-07 编排（作业种类、进度与单位写入）。"""
    repository = FakeMaterializationRepository()
    flight_repository = FakeFlightRepository(
        pages=[["100001", "100002"]],
        basis_by_id={"100001": BASIS, "100002": BASIS},
    )

    outcome = await run_flight_phase_segments_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        progress_interval=1,
    )

    assert outcome.kind == "phase_segments"
    assert outcome.status == "succeeded"
    assert outcome.succeeded_count == 2
    assert outcome.failed == []
    assert [kind for kind, _ in repository.records] == ["phase_segments", "phase_segments"]
    latest = repository.job_history[-1]
    assert latest["kind"] == "phase_segments"
    assert latest["status"] == "succeeded"
    assert latest["algorithm_version"] == FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION


@pytest.mark.asyncio
async def test_run_flight_features_job_marks_partial_on_isolated_failure():
    repository = FakeMaterializationRepository()
    flight_repository = FakeFlightRepository(
        pages=[["100001", "100002"]],
        basis_by_id={"100001": BASIS},
    )

    outcome = await run_flight_features_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        job_id="job-2",
        generated_at=GENERATED,
    )

    assert outcome.status == "partial"
    assert outcome.succeeded_count == 1
    assert outcome.failed == [
        {"external_id": "100002", "error": "航班无有效轨迹点: scat20161015_20161021/100002"}
    ]
    final = repository.job_documents["job-2"]
    assert final["status"] == "partial"
    assert final["failed_targets"] == ["100002"]
    assert "100002" in final["failure_detail"]


@pytest.mark.asyncio
async def test_run_flight_features_job_marks_failed_when_every_unit_fails():
    repository = FakeMaterializationRepository()
    flight_repository = FakeFlightRepository(pages=[["100002"]], basis_by_id={})

    outcome = await run_flight_features_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        job_id="job-3",
        generated_at=GENERATED,
    )

    assert outcome.status == "failed"
    assert repository.job_documents["job-3"]["status"] == "failed"
    assert repository.job_documents["job-3"]["progress"] == 1.0


@pytest.mark.asyncio
async def test_run_flight_features_job_skips_existing_incrementally():
    repository = FakeMaterializationRepository(existing={"100001"})
    flight_repository = FakeFlightRepository(
        pages=[["100001", "100002"]],
        basis_by_id={"100002": BASIS},
    )

    outcome = await run_flight_features_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        job_id="job-4",
        generated_at=GENERATED,
    )

    # 已物化航班不重复计算：计划量 = 批次航班数 - 已有结果数
    assert outcome.total_count == 1
    assert outcome.skipped_count == 1
    assert outcome.succeeded_count == 1
    assert repository.job_documents["job-4"]["progress"] == 1.0


@pytest.mark.asyncio
async def test_run_flight_features_job_retries_previous_failures():
    repository = FakeMaterializationRepository(
        jobs={
            "job-old": {
                "job_id": "job-old",
                "kind": "flight_features",
                "failed_targets": ["100002"],
            }
        },
        existing={"100001"},
    )
    flight_repository = FakeFlightRepository(basis_by_id={"100002": BASIS})

    outcome = await run_flight_features_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        job_id="job-5",
        retry_of="job-old",
        generated_at=GENERATED,
    )

    assert outcome.succeeded_count == 1
    assert outcome.total_count == 1
    # 重试只处理失败单位，不扫描整批
    flight_repository.collection.find.assert_not_called()
    assert repository.job_documents["job-5"]["retry_of"] == "job-old"


@pytest.mark.asyncio
async def test_run_flight_features_job_force_recomputes_and_respects_limit():
    repository = FakeMaterializationRepository(existing={"100001"})
    flight_repository = FakeFlightRepository(
        pages=[["100001", "100002"]],
        basis_by_id={"100001": BASIS, "100002": BASIS},
    )

    outcome = await run_flight_features_job(
        batch=BATCH,
        flight_repository=flight_repository,
        materialization_repository=repository,
        job_id="job-6",
        force=True,
        limit=1,
        generated_at=GENERATED,
    )

    assert outcome.total_count == 1
    assert outcome.skipped_count == 0
    assert outcome.succeeded_count == 1
    assert outcome.processed_count == 1


@pytest.mark.asyncio
async def test_failed_targets_of_validates_job():
    repository = FakeMaterializationRepository(
        jobs={
            "job-old": {
                "job_id": "job-old",
                "kind": "flight_features",
                "failed_targets": ["100002", "100003"],
            },
            "job-stats": {"job_id": "job-stats", "kind": "batch_statistics"},
        }
    )

    assert await failed_targets_of(repository, "job-old", kind="flight_features") == [
        "100002",
        "100003",
    ]
    with pytest.raises(ValueError, match="任务不存在"):
        await failed_targets_of(repository, "job-missing")
    with pytest.raises(ValueError, match="不能用于"):
        await failed_targets_of(repository, "job-stats", kind="flight_features")


def test_new_job_id_is_semantic_and_bounded():
    job_id = new_job_id("flight_features", "scat20161015_20161021/../bad name!", now=GENERATED)

    assert job_id.startswith("flight_features-scat20161015_20161021")
    assert len(job_id) <= 100
    assert "/" not in job_id and " " not in job_id


STATISTICS: dict[str, Any] = {
    "batch": BATCH,
    "flight_count": 2,
    "unique_callsign_count": 2,
    "aircraft_type_count": 1,
    "departure_airport_count": 1,
    "arrival_airport_count": 1,
    "total_track_points": 240,
    "average_track_points": 120.0,
    "minimum_track_points": 110,
    "maximum_track_points": 130,
    "flights_with_prediction": 1,
    "total_prediction_snapshots": 5,
}

QUALITY: dict[str, Any] = {
    "missing_callsign_count": 0,
    "missing_aircraft_type_count": 0,
    "missing_departure_airport_count": 0,
    "missing_arrival_airport_count": 0,
    "missing_iobt_count": 0,
    "empty_trajectory_count": 0,
    "flights_without_prediction_count": 1,
}


class FakeStatisticsFlightRepository(FakeFlightRepository):
    """只提供批次统计物化所需的两个聚合接口。"""

    def __init__(self, *, statistics: dict[str, Any] | None) -> None:
        super().__init__(pages=[])
        self._statistics = statistics

    async def get_batch_statistics(self, *, batch: str) -> dict[str, Any] | None:
        return self._statistics

    async def get_batch_data_quality(self, *, batch: str) -> dict[str, Any]:
        return dict(QUALITY)


@pytest.mark.asyncio
async def test_run_batch_statistics_job_records_success():
    repository = FakeMaterializationRepository()

    outcome = await run_batch_statistics_job(
        batch=BATCH,
        flight_repository=FakeStatisticsFlightRepository(statistics=STATISTICS),
        materialization_repository=repository,
        job_id="job-stats",
        generated_at=GENERATED,
    )

    assert outcome.status == "succeeded"
    assert repository.records == [("batch_statistics", "")]
    assert [item["status"] for item in repository.job_history] == ["running", "succeeded"]
    assert repository.job_documents["job-stats"]["progress"] == 1.0


@pytest.mark.asyncio
async def test_run_batch_statistics_job_marks_failed_and_reraises():
    repository = FakeMaterializationRepository()

    with pytest.raises(ValueError, match="批次不存在或无数据"):
        await run_batch_statistics_job(
            batch="no-such-batch",
            flight_repository=FakeStatisticsFlightRepository(statistics=None),
            materialization_repository=repository,
            job_id="job-stats-bad",
            generated_at=GENERATED,
        )

    document = repository.job_documents["job-stats-bad"]
    assert document["status"] == "failed"
    assert "批次不存在或无数据" in document["error_message"]
