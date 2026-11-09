"""M6-08 实验编排测试：pending 生命周期、experiment-agent 图与边界工具。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pymongo import ReturnDocument

from agents import experiment_tools as tools
from agents.experiment_agent import experiment_agent, public_experiment_agent_result
from agents.experiment_agent import graph as graph_module
from scat.experiments.artifacts import ExperimentArtifactStore
from scat.experiments.contracts import ExperimentRecord
from scat.experiments.evaluation import EvaluationConfig
from scat.experiments.repository import ExperimentRepository
from scat.experiments.runner import create_pending_experiment, run_pending_experiment
from scat.prediction.geo import local_to_geodetic_m

BATCH = "scat20161015_20161021"
T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)


def _repository() -> tuple[ExperimentRepository, MagicMock]:
    database = MagicMock()
    collection = MagicMock()
    database.__getitem__.return_value = collection
    return ExperimentRepository(database), collection


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
    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    def sort(self, *args: object, **kwargs: object) -> _FakeCursor:
        return self

    def limit(self, count: int) -> _FakeCursor:
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        return list(self._documents)


def _pending_record() -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id="m6eval-kalman-cv-b-20260920T000000-abc123",
        status="pending",
        batch=BATCH,
        predictor_id="kalman-cv",
        predictor_version="1",
        model_version="1",
        data_version="scat-import-2",
        feature_version="predictor-schema-v1",
        config={
            "horizons_s": [60.0, 180.0],
            "stride_s": 120.0,
            "max_cutoffs_per_flight": 3,
            "min_observed_s": 60.0,
            "min_observed_points": 3,
            "max_actual_gap_s": 300.0,
            "limit": 2,
        },
        source="chat",
        requested_by="u1",
        code_version="0.1.0",
        created_at=T0,
    )


@pytest.mark.asyncio
async def test_claim_next_targets_oldest_pending() -> None:
    repository, collection = _repository()
    collection.find_one_and_update = AsyncMock(
        return_value={"experiment_id": "e1", "status": "running", "lease_token": "a" * 32}
    )

    document = await repository.claim_next(
        lease_owner="worker-1",
        lease_ttl=timedelta(minutes=5),
        now=T0,
    )

    assert document == {"experiment_id": "e1", "status": "running", "lease_token": "a" * 32}
    await_args = collection.find_one_and_update.await_args
    assert await_args.args[0]["status"] == "pending"
    assert await_args.args[0]["$and"][0]["$or"][2] == {"next_attempt_at": {"$lte": T0}}
    lease_fields = await_args.args[1]["$set"]
    assert lease_fields["status"] == "running"
    assert lease_fields["lease_owner"] == "worker-1"
    assert len(lease_fields["lease_token"]) == 32
    assert lease_fields["lease_acquired_at"] == T0
    assert lease_fields["lease_heartbeat_at"] == T0
    assert lease_fields["lease_expires_at"] == T0 + timedelta(minutes=5)
    assert await_args.args[1]["$inc"] == {"attempt_count": 1}
    assert await_args.kwargs["sort"] == [("created_at", 1)]
    assert await_args.kwargs["return_document"] == ReturnDocument.AFTER


@pytest.mark.asyncio
async def test_claim_filters_by_experiment_id() -> None:
    repository, collection = _repository()
    collection.find_one_and_update = AsyncMock(return_value=None)

    assert await repository.claim("e2", lease_owner="worker-2", now=T0) is None

    await_args = collection.find_one_and_update.await_args
    assert await_args.args[0]["experiment_id"] == "e2"
    assert await_args.args[0]["status"] == "pending"
    assert await_args.args[1]["$set"]["lease_owner"] == "worker-2"
    assert "sort" not in await_args.kwargs


@pytest.mark.asyncio
async def test_heartbeat_requires_current_lease_token_and_extends_expiry() -> None:
    repository, collection = _repository()
    collection.find_one_and_update = AsyncMock(
        side_effect=[{"experiment_id": "e1", "status": "running"}, None]
    )

    assert await repository.heartbeat(
        "e1",
        lease_token="current-token",
        lease_ttl=timedelta(minutes=5),
        now=T0,
    )
    assert not await repository.heartbeat(
        "e1",
        lease_token="stale-token",
        lease_ttl=timedelta(minutes=5),
        now=T0,
    )

    first_call = collection.find_one_and_update.await_args_list[0]
    assert first_call.args[0] == {
        "experiment_id": "e1",
        "status": "running",
        "lease_token": "current-token",
    }
    assert first_call.args[1]["$set"] == {
        "lease_heartbeat_at": T0,
        "lease_expires_at": T0 + timedelta(minutes=5),
    }


@pytest.mark.asyncio
async def test_create_pending_experiment_persists_pending_record() -> None:
    repository, collection = _repository()
    collection.replace_one = AsyncMock()

    record = await create_pending_experiment(
        config=EvaluationConfig(batch=BATCH, predictor_id="kalman-cv", horizons_s=(60.0,)),
        experiment_repository=repository,
        limit=5,
        requested_by="u1",
        source="chat",
    )

    assert record.status == "pending"
    assert record.config["limit"] == 5
    assert record.source == "chat"
    assert record.requested_by == "u1"
    assert record.experiment_id.startswith("m6eval-kalman-cv-")
    document = collection.replace_one.await_args.args[1]
    assert document["status"] == "pending"
    assert document["config"]["horizons_s"] == [60.0]


@pytest.mark.asyncio
async def test_run_pending_experiment_claims_and_executes(tmp_path) -> None:
    repository, collection = _repository()
    document = _pending_record().model_dump(mode="json")
    document["status"] = "running"
    document["lease_owner"] = "test-worker"
    document["lease_token"] = "a" * 32
    document["lease_acquired_at"] = T0.isoformat()
    document["lease_heartbeat_at"] = T0.isoformat()
    document["lease_expires_at"] = (T0 + timedelta(minutes=5)).isoformat()
    document["attempt_count"] = 1
    collection.find_one_and_update = AsyncMock(return_value=document)
    collection.replace_one = AsyncMock(return_value=SimpleNamespace(matched_count=1))
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
    flight_repository.get_evaluation_inputs = AsyncMock(
        return_value={"plots": _plots(), "snapshots": []}
    )

    record = await run_pending_experiment(
        experiment_repository=repository,
        flight_repository=flight_repository,
        experiment_id="m6eval-kalman-cv-b-20260920T000000-abc123",
        phase_grouping=False,
        artifact_store=ExperimentArtifactStore(tmp_path),
    )

    assert record is not None
    assert record.status == "succeeded"
    assert record.dataset["flight_count"] == 2
    assert record.coverage["flights_with_samples"] == 2
    assert all(metric.sample_count > 0 for metric in record.metrics)
    assert record.timing["predict_calls"] > 0
    assert collection.replace_one.await_count == 1
    finish_filter = collection.replace_one.await_args.args[0]
    assert finish_filter == {
        "experiment_id": record.experiment_id,
        "status": "running",
        "lease_token": "a" * 32,
    }
    assert ".attempt-1." in record.artifacts[0]["path"]


@pytest.mark.asyncio
async def test_run_pending_experiment_renews_lease_while_running(tmp_path) -> None:
    repository, collection = _repository()
    claimed = _pending_record().model_dump(mode="json")
    claimed.update(
        {
            "status": "running",
            "lease_owner": "test-worker",
            "lease_token": "b" * 32,
            "lease_acquired_at": T0.isoformat(),
            "lease_heartbeat_at": T0.isoformat(),
            "lease_expires_at": (T0 + timedelta(seconds=1)).isoformat(),
            "attempt_count": 1,
        }
    )

    async def find_one_and_update(filters: dict, update: dict, **_kwargs: object) -> dict | None:
        if filters.get("status") == "pending":
            return claimed
        if filters.get("lease_token") == claimed["lease_token"]:
            return {**claimed, **update["$set"]}
        return None

    collection.find_one_and_update = AsyncMock(side_effect=find_one_and_update)
    collection.replace_one = AsyncMock(return_value=SimpleNamespace(matched_count=1))
    flight_repository = Mock()
    flight_repository.collection.find = Mock(
        side_effect=[
            _FakeCursor([{"_id": 1, "source": {"external_id": "f1"}}]),
            _FakeCursor([]),
        ]
    )

    async def slow_inputs(**_kwargs: object) -> dict:
        await asyncio.sleep(0.04)
        return {"plots": _plots(), "snapshots": []}

    flight_repository.get_evaluation_inputs = AsyncMock(side_effect=slow_inputs)

    record = await run_pending_experiment(
        experiment_repository=repository,
        flight_repository=flight_repository,
        experiment_id=claimed["experiment_id"],
        phase_grouping=False,
        artifact_store=ExperimentArtifactStore(tmp_path),
        lease_owner="test-worker",
        lease_ttl_seconds=1.0,
        heartbeat_interval_seconds=0.01,
    )

    assert record is not None
    assert record.status == "succeeded"
    heartbeat_calls = [
        call
        for call in collection.find_one_and_update.await_args_list
        if call.args[0].get("lease_token") == claimed["lease_token"]
    ]
    assert heartbeat_calls
    assert heartbeat_calls[-1].args[1]["$set"]["lease_expires_at"] > T0


@pytest.mark.asyncio
async def test_run_pending_experiment_returns_none_without_pending() -> None:
    repository, collection = _repository()
    collection.find_one_and_update = AsyncMock(return_value=None)

    assert (
        await run_pending_experiment(
            experiment_repository=repository,
            flight_repository=Mock(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_experiment_agent_rejects_invalid_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graph_module, "ExperimentRepository", MagicMock())
    monkeypatch.setattr(graph_module, "flight_database", type("F", (), {"db": object()})())

    missing_batch = await experiment_agent.ainvoke(
        {"request": {"experiment_kind": "predictor_evaluation"}}
    )
    assert public_experiment_agent_result(missing_batch)["error_code"] == "invalid_request"

    unknown_kind = await experiment_agent.ainvoke({"request": {"experiment_kind": "training"}})
    assert (
        public_experiment_agent_result(unknown_kind)["error_code"] == "unsupported_experiment_kind"
    )


@pytest.mark.asyncio
async def test_experiment_agent_unknown_predictor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graph_module, "ExperimentRepository", MagicMock())
    monkeypatch.setattr(graph_module, "flight_database", type("F", (), {"db": object()})())

    state = await experiment_agent.ainvoke(
        {
            "request": {
                "experiment_kind": "predictor_evaluation",
                "batch": BATCH,
                "predictor_id": "nope",
            }
        }
    )

    result = public_experiment_agent_result(state)
    assert result["status"] == "failed"
    assert result["error_code"] == "unknown_predictor"


@pytest.mark.asyncio
async def test_experiment_agent_persists_pending_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeRepository:
        def __init__(self, database: object) -> None:
            captured["database"] = database

        async def upsert(self, record: ExperimentRecord) -> None:
            captured["record"] = record

    monkeypatch.setattr(graph_module, "ExperimentRepository", _FakeRepository)
    monkeypatch.setattr(graph_module, "flight_database", type("F", (), {"db": object()})())

    state = await experiment_agent.ainvoke(
        {
            "request": {
                "experiment_kind": "predictor_evaluation",
                "batch": BATCH,
                "predictor_id": "kalman-cv",
                "limit": 3,
            },
            "requested_by": "u1",
        }
    )

    result = public_experiment_agent_result(state)
    assert result["status"] == "queued"
    assert set(result) == {
        "run_id",
        "status",
        "progress",
        "message",
        "error_code",
        "result_summary",
    }
    record = captured["record"]
    assert isinstance(record, ExperimentRecord)
    assert record.status == "pending"
    assert record.config["limit"] == 3
    assert record.source == "chat"
    assert record.requested_by == "u1"
    assert result["result_summary"]["experiment_id"] == record.experiment_id


def _patch_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents import scat_tools

    fake_database = type("F", (), {"db": Mock(), "connect": AsyncMock()})()
    monkeypatch.setattr(scat_tools, "flight_database", fake_database)
    monkeypatch.setattr(tools, "flight_database", fake_database)


@pytest.mark.asyncio
async def test_prepare_prediction_experiment_returns_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flight_repository = Mock()
    flight_repository.count_all = AsyncMock(return_value=123)
    monkeypatch.setattr(tools, "ScatFlightRepository", lambda: flight_repository)
    _patch_database(monkeypatch)

    result = await tools.prepare_prediction_experiment.ainvoke(
        {"batch": BATCH, "predictor_id": "kalman-cv", "limit": 50},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "awaiting_confirmation"
    assert result["confirmation_id"]
    assert result["data"]["batch_flight_count"] == 123
    assert result["data"]["planned_flight_count"] == 50
    assert result["data"]["predictor_version"] == "1"
    assert result["data"]["protocol_version"] == "m6-eval-v1"


@pytest.mark.asyncio
async def test_prepare_unknown_predictor_is_invalid_request() -> None:
    result = await tools.prepare_prediction_experiment.ainvoke(
        {"batch": BATCH, "predictor_id": "nope"},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "invalid_request"
    assert "未知预测器" in result["message"]


@pytest.mark.asyncio
async def test_submit_invalid_confirmation_is_stable_error() -> None:
    result = await tools.submit_prediction_experiment.ainvoke(
        {"confirmation_id": "nope"},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "invalid_confirmation"
    assert result["data"] is None


@pytest.mark.asyncio
async def test_cancel_prepared_experiment_revokes_confirmation() -> None:
    prepared = tools.PreparedPredictionExperiment(
        batch=BATCH,
        predictor_id="kalman-cv",
        predictor_version="1",
        limit=7,
        stride_s=60.0,
        max_cutoffs_per_flight=12,
        horizons_s=(60.0, 180.0),
        batch_flight_count=100,
    )
    confirmation_id, _ = tools.confirmation_store.issue(
        prepared,
        thread_id="cancel-t1",
        user_id="cancel-u1",
    )

    result = await tools.cancel_prediction_experiment_submission.ainvoke(
        {"confirmation_id": confirmation_id},
        config={"configurable": {"thread_id": "cancel-t1", "user_id": "cancel-u1"}},
    )

    assert result["status"] == "cancelled"
    assert (
        tools.confirmation_store.consume(
            confirmation_id,
            thread_id="cancel-t1",
            user_id="cancel-u1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_submit_consumes_confirmation_and_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = tools.PreparedPredictionExperiment(
        batch=BATCH,
        predictor_id="kalman-cv",
        predictor_version="1",
        limit=7,
        stride_s=60.0,
        max_cutoffs_per_flight=12,
        horizons_s=(60.0, 180.0),
        batch_flight_count=100,
    )
    confirmation_id, _expires_at = tools.confirmation_store.issue(
        prepared,
        thread_id="t1",
        user_id="u1",
    )
    fake_agent = Mock()
    fake_agent.ainvoke = AsyncMock(
        return_value={
            "run_id": "r1",
            "status": "queued",
            "progress": 1.0,
            "result_summary": {
                "experiment_id": "m6eval-kalman-cv-x-1-abc123",
                "batch": BATCH,
            },
        }
    )
    monkeypatch.setattr(tools, "experiment_agent", fake_agent)
    _patch_database(monkeypatch)

    result = await tools.submit_prediction_experiment.ainvoke(
        {"confirmation_id": confirmation_id},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "queued"
    assert result["data"]["experiment_id"] == "m6eval-kalman-cv-x-1-abc123"
    assert "claim --experiment-id m6eval-kalman-cv-x-1-abc123" in result["data"]["execution"]
    assert result["data"]["job_status"] == "pending"

    again = await tools.submit_prediction_experiment.ainvoke(
        {"confirmation_id": confirmation_id},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )
    assert again["status"] == "invalid_confirmation"


def test_confirmation_store_enforces_thread_scope() -> None:
    prepared = tools.PreparedPredictionExperiment(
        batch=BATCH,
        predictor_id="kalman-cv",
        predictor_version="1",
        limit=None,
        stride_s=60.0,
        max_cutoffs_per_flight=12,
        horizons_s=(60.0,),
        batch_flight_count=10,
    )
    confirmation_id, _expires_at = tools.confirmation_store.issue(
        prepared,
        thread_id="t1",
        user_id="u1",
    )

    assert tools.confirmation_store.consume(confirmation_id, thread_id="t2", user_id="u1") is None
    assert (
        tools.confirmation_store.consume(confirmation_id, thread_id="t1", user_id="u1") is prepared
    )
    assert tools.confirmation_store.consume(confirmation_id, thread_id="t1", user_id="u1") is None


@pytest.mark.asyncio
async def test_get_experiment_run_not_found_and_shapes_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, collection = _repository()
    monkeypatch.setattr(tools, "ExperimentRepository", lambda database: repository)
    _patch_database(monkeypatch)

    collection.find_one = AsyncMock(return_value=None)
    missing = await tools.get_experiment_run.ainvoke({"experiment_id": "e1"})
    assert missing["status"] == "not_found"

    collection.find_one = AsyncMock(
        return_value={
            "experiment_id": "e1",
            "status": "succeeded",
            "batch": BATCH,
            "predictor_id": "kalman-cv",
            "predictor_version": "1",
            "metrics": [{"horizon_s": 60.0, "sample_count": 10}],
            "worst_samples": [{"external_id": f"f{index}"} for index in range(8)],
            "dataset": {"flight_count": 2},
            "coverage": {"flights_processed": 2},
        }
    )
    found = await tools.get_experiment_run.ainvoke({"experiment_id": "e1"})

    assert found["status"] == "ok"
    assert found["data"]["predictor"] == {"id": "kalman-cv", "version": "1"}
    assert len(found["data"]["worst_samples"]) == 5
    assert found["data"]["metrics"][0]["sample_count"] == 10


@pytest.mark.asyncio
async def test_list_prediction_experiments_shapes_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, collection = _repository()
    monkeypatch.setattr(tools, "ExperimentRepository", lambda database: repository)
    _patch_database(monkeypatch)
    cursor = Mock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[{"experiment_id": "e1", "status": "pending"}])
    collection.find = Mock(return_value=cursor)

    result = await tools.list_prediction_experiments.ainvoke({"limit": 5})

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["experiments"][0]["experiment_id"] == "e1"
