"""M6 实验仓储与 runner 测试：记录写入、索引、评估归档（mock 数据库）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from scat.experiments.artifacts import ExperimentArtifactStore
from scat.experiments.contracts import ExperimentRecord
from scat.experiments.evaluation import EvaluationConfig
from scat.experiments.repository import ExperimentRepository
from scat.experiments.runner import run_predictor_evaluation
from scat.prediction.geo import local_to_geodetic_m

T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)


def _record() -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id="exp-1",
        status="succeeded",
        batch="b",
        predictor_id="constant-velocity-heading",
        predictor_version="1",
        model_version="1",
        data_version="scat-import-2",
        feature_version="predictor-schema-v1",
        code_version="0.1.0",
        created_at=T0,
    )


def _cursor(rows: list[dict]) -> Mock:
    cursor = Mock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def _repository() -> tuple[ExperimentRepository, MagicMock]:
    database = MagicMock()
    collection = MagicMock()
    database.__getitem__.return_value = collection
    return ExperimentRepository(database), collection


@pytest.mark.asyncio
async def test_upsert_replaces_by_experiment_id() -> None:
    repository, collection = _repository()
    collection.replace_one = AsyncMock()

    await repository.upsert(_record())

    await_args = collection.replace_one.await_args
    assert await_args is not None
    assert await_args.args[0] == {"experiment_id": "exp-1"}
    assert await_args.kwargs["upsert"] is True
    assert await_args.args[1]["predictor_id"] == "constant-velocity-heading"


@pytest.mark.asyncio
async def test_ensure_indexes_creates_unique_and_query_indexes() -> None:
    repository, collection = _repository()
    collection.create_index = AsyncMock(
        side_effect=[
            "uq_model_experiments_id",
            "idx_model_experiments_batch_predictor",
            "idx_model_experiments_status_lease_expiry",
        ]
    )

    created = await repository.ensure_indexes()

    assert created == [
        "uq_model_experiments_id",
        "idx_model_experiments_batch_predictor",
        "idx_model_experiments_status_lease_expiry",
    ]
    first_call = collection.create_index.await_args_list[0]
    assert first_call.args[0] == [("experiment_id", 1)]
    assert first_call.kwargs == {"unique": True, "name": "uq_model_experiments_id"}
    second_call = collection.create_index.await_args_list[1]
    assert second_call.args[0] == [("batch", 1), ("predictor_id", 1), ("created_at", -1)]
    third_call = collection.create_index.await_args_list[2]
    assert third_call.args[0] == [("status", 1), ("lease_expires_at", 1)]
    assert third_call.kwargs == {"name": "idx_model_experiments_status_lease_expiry"}


@pytest.mark.asyncio
async def test_list_recent_filters_and_clamps_limit() -> None:
    repository, collection = _repository()
    cursor = _cursor([{"experiment_id": "exp-1"}])
    collection.find = Mock(return_value=cursor)

    rows = await repository.list_recent(batch="b", predictor_id="p", limit=500)

    assert rows == [{"experiment_id": "exp-1"}]
    filters, projection = collection.find.call_args.args
    assert filters == {"batch": "b", "predictor_id": "p"}
    assert projection["experiment_id"] == 1
    cursor.limit.assert_called_once_with(100)
    cursor.sort.assert_called_once_with("created_at", -1)


def _plots(*, count: int = 60, step_s: float = 10.0) -> list[dict]:
    plots: list[dict] = []
    for index in range(count):
        lat, lon, _ = local_to_geodetic_m(
            120.0 * step_s * index,
            0.0,
            0.0,
            origin_lat=59.0,
            origin_lon=18.0,
            origin_alt_m=10000.0,
        )
        plots.append(
            {
                "timestamp": (T0 + timedelta(seconds=step_s * index)).isoformat(),
                "lat": lat,
                "lon": lon,
                "altitude_m": 10000.0,
            }
        )
    return plots


class _FakeCursor:
    """支撑 iter_batch_external_ids 的游标链（sort/limit/to_list）。"""

    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    def sort(self, *args: object, **kwargs: object) -> _FakeCursor:
        return self

    def limit(self, count: int) -> _FakeCursor:
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        return list(self._documents)


def _flight_repository(*, get_inputs_side_effect: object) -> Mock:
    flight_repository = Mock()
    flight_repository.collection.find = Mock(
        side_effect=[
            _FakeCursor(
                [
                    {"_id": 1, "source": {"external_id": "f1"}},
                    {"_id": 2, "source": {"external_id": "f2"}},
                ]
            ),
            _FakeCursor([]),
        ]
    )
    flight_repository.get_evaluation_inputs = AsyncMock(side_effect=get_inputs_side_effect)
    return flight_repository


@pytest.mark.asyncio
async def test_runner_evaluates_flights_and_persists_record(tmp_path) -> None:
    flight_repository = _flight_repository(
        get_inputs_side_effect=[{"plots": _plots(), "snapshots": []}] * 2
    )
    repository, collection = _repository()
    collection.replace_one = AsyncMock()

    record = await run_predictor_evaluation(
        config=EvaluationConfig(
            batch="b",
            predictor_id="constant-velocity-heading",
            horizons_s=(60.0, 180.0),
            stride_s=120.0,
            max_cutoffs_per_flight=3,
        ),
        flight_repository=flight_repository,
        experiment_repository=repository,
        phase_grouping=False,
        artifact_store=ExperimentArtifactStore(tmp_path),
    )

    assert record.status == "succeeded"
    assert record.dataset["flight_count"] == 2
    assert record.dataset["sample_flight_ids"] == ["f1", "f2"]
    assert record.dataset["external_ids_sha256"]
    assert record.coverage["flights_with_samples"] == 2
    by_horizon = {metric.horizon_s: metric for metric in record.metrics}
    assert by_horizon[60.0].sample_count > 0
    assert by_horizon[180.0].sample_count > 0
    assert by_horizon[60.0].mae_m is not None
    assert by_horizon[60.0].mae_m < 200.0  # 直线轨迹上基线应保持百米以内
    assert record.finished_at is not None
    # 报告完整性：耗时与适用范围 + 产物文件登记
    assert record.timing["predict_calls"] > 0
    assert record.timing["duration_s"] >= 0
    assert record.applicability
    artifact = record.artifacts[0]
    assert artifact["name"] == "evaluation_report.json"
    assert artifact["status"] == "stored"
    assert (tmp_path / record.experiment_id / "evaluation_report.json").exists()
    # running 与最终记录各写一次
    assert collection.replace_one.await_count == 2


@pytest.mark.asyncio
async def test_runner_groups_metrics_by_phase(tmp_path) -> None:
    flight_repository = _flight_repository(
        get_inputs_side_effect=[{"plots": _plots(), "snapshots": []}] * 2
    )
    repository, collection = _repository()
    collection.replace_one = AsyncMock()
    segments = [
        {
            "start_time": T0.isoformat(),
            "end_time": (T0 + timedelta(seconds=240)).isoformat(),
            "phase": "climb",
        },
        {
            "start_time": (T0 + timedelta(seconds=240)).isoformat(),
            "end_time": (T0 + timedelta(seconds=600)).isoformat(),
            "phase": "high_cruise",
        },
    ]

    async def phase_lookup(_external_id: str) -> list[dict] | None:
        return segments

    record = await run_predictor_evaluation(
        config=EvaluationConfig(
            batch="b",
            predictor_id="constant-velocity-heading",
            horizons_s=(60.0,),
            stride_s=120.0,
            max_cutoffs_per_flight=3,
        ),
        flight_repository=flight_repository,
        experiment_repository=repository,
        phase_lookup=phase_lookup,
        artifact_store=ExperimentArtifactStore(tmp_path),
    )

    assert {row.phase for row in record.phase_metrics} == {"climb", "high_cruise"}
    assert all(row.sample_count > 0 for row in record.phase_metrics)
    assert record.coverage["phase_grouping"]["flights_with_segments"] == 2
    assert record.coverage["phase_grouping"]["ungrouped_samples"] == 0


@pytest.mark.asyncio
async def test_runner_records_failed_flights_without_aborting(tmp_path) -> None:
    flight_repository = _flight_repository(
        get_inputs_side_effect=[RuntimeError("boom"), {"plots": _plots(), "snapshots": []}]
    )
    repository, collection = _repository()
    collection.replace_one = AsyncMock()

    record = await run_predictor_evaluation(
        config=EvaluationConfig(
            batch="b",
            predictor_id="constant-velocity-heading",
            horizons_s=(60.0,),
            stride_s=300.0,
            max_cutoffs_per_flight=1,
        ),
        flight_repository=flight_repository,
        experiment_repository=repository,
        phase_grouping=False,
        artifact_store=ExperimentArtifactStore(tmp_path),
    )

    assert record.status == "partial"
    assert record.coverage["failed_flights"] == 1
    assert record.coverage["failed_flight_ids"] == ["f1"]
    assert record.coverage["flights_with_samples"] == 1
