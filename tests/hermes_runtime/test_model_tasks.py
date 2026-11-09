"""H5 API -> durable confirmation -> worker -> original graph -> artifact contract."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError

from agents import model_tools as legacy
from agents.model_agent import build_model_agent
from analysis_runs import AnalysisRunConflictError, AnalysisRunStatusEvent
from application.context import ExecutionContext
from application.model_tasks import ModelTasks, TaskProposal
from application.model_worker import reconcile, run_once
from application.task_store import TaskConflict, TaskStore
from core import settings
from core.settings import AnalysisIdentityMode
from hermes_runtime.registry import RunRegistry
from scat_model_runtime import (
    ComponentReference,
    ComponentRegistration,
    ComponentRegistry,
    DatasetKind,
    ModelAvailability,
    ModelExecutionMode,
    ModelRegistry,
    ModelResourceLimits,
    ModelRunStatus,
    ModelSpec,
    ModelTaskType,
    RegressionInputSeries,
    RegressionPredictionSeries,
    RegressionTaskInput,
    RegressionTaskOutput,
)
from service.hermes_api import get_hermes_owner
from service.model_task_api import router


class Runs:
    def __init__(self):
        self.rows = {}

    async def create(self, record):
        assert record.run_id not in self.rows
        self.rows[record.run_id] = record

    async def get_for_owner(self, run_id, *, owner_user_id):
        row = self.rows.get(run_id)
        return row if row and row.owner_user_id == owner_user_id else None

    async def transition(self, run_id, *, expected_statuses, status, progress, **kwargs):
        row = self.rows[run_id]
        if row.status not in expected_statuses:
            raise AnalysisRunConflictError("status changed")
        now = datetime.now(UTC)
        updates = {
            k: v
            for k, v in kwargs.items()
            if k in {"error", "result_summary", "result_schema_version", "result_artifact_ids"}
        }
        updates.update(
            status=status,
            progress=progress,
            updated_at=now,
            status_history=row.status_history
            + (AnalysisRunStatusEvent(status=status, progress=progress, at=now),),
        )
        if status == ModelRunStatus.RUNNING:
            updates["started_at"] = now
        if status in {ModelRunStatus.FAILED, ModelRunStatus.SUCCEEDED}:
            updates["finished_at"] = now
        self.rows[run_id] = row.model_copy(update=updates)


class Artifacts:
    def __init__(self):
        self.rows = {}

    async def create(self, record):
        self.rows[record.artifact_id] = record

    async def get_for_owner(self, artifact_id, *, owner_user_id):
        row = self.rows.get(artifact_id)
        return row if row and row.owner_user_id == owner_user_id else None


class Resolver:
    async def resolve(self, *, request):
        ref = request.primary_input_ref
        now = datetime(2020, 1, 1, tzinfo=UTC)
        return RegressionTaskInput(
            feature_names=("lat", "lon", "alt"),
            feature_units={"lat": "degree", "lon": "degree", "alt": "metre"},
            series=(
                RegressionInputSeries(
                    input_ref=ref,
                    timestamps=(now, now + timedelta(seconds=10)),
                    values=((60.0, 20.0, 1000.0), (60.1, 20.1, 1100.0)),
                ),
            ),
        )


class Package:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def start(self):
        pass

    def stop(self):
        pass

    def run(self, *, model_input, parameters):
        self.calls += 1
        if self.fail:
            raise RuntimeError("sensitive-private-path")
        return RegressionTaskOutput(
            model_key="h5-test",
            model_version="1",
            target_names=("lat", "lon", "alt"),
            target_units={"lat": "degree", "lon": "degree", "alt": "metre"},
            series=(
                RegressionPredictionSeries(
                    input_ref=model_input.series[0].input_ref,
                    forecast_origin=model_input.series[0].timestamps[-1],
                    time_offsets_seconds=(10.0,),
                    values=((60.2, 20.2, 1200.0),),
                ),
            ),
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    resolver = ComponentReference(key="h5-resolver", version="1")
    package = ComponentReference(key="h5-package", version="1")
    spec = ModelSpec(
        model_key="h5-test",
        version="1",
        display_name="H5 controlled fixture",
        description="No GPU or external model",
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        supported_datasets={DatasetKind.SCAT},
        input_schema="regression-task-input/v1",
        output_schema="regression-task-output/v1",
        input_resolver=resolver,
        model_package=package,
        execution_mode=ModelExecutionMode.ASYNC,
        resource_limits=ModelResourceLimits(
            min_input_points=2, max_input_points=10, max_output_points=2, timeout_seconds=5
        ),
        availability=ModelAvailability.AVAILABLE,
        is_default=True,
    )
    models = ModelRegistry([spec])
    resolvers = ComponentRegistry("h5-resolvers")
    packages = ComponentRegistry("h5-packages")
    backend = Package()
    resolvers.register(ComponentRegistration(resolver, Resolver()))
    packages.register(ComponentRegistration(package, backend))
    graph = build_model_agent(
        models=models, regression_input_resolvers=resolvers, regression_model_packages=packages
    )
    runs = Runs()
    artifacts = Artifacts()
    monkeypatch.setattr(legacy, "model_agent", graph)
    monkeypatch.setattr(legacy, "analysis_run_repository", runs)
    monkeypatch.setattr(legacy, "analysis_artifact_repository", artifacts)
    app = ModelTasks(
        TaskStore(tmp_path / "tasks.sqlite", workspace_root=tmp_path),
        models=models,
        runs=runs,
        artifacts=artifacts,
    )
    context = ExecutionContext("owner", "thread")
    proposal = TaskProposal(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="h5-test",
        input_ref={"dataset": "scat", "batch": "batch", "external_id": "flight-1", "limit": 2},
    )
    return SimpleNamespace(
        app=app,
        context=context,
        proposal=proposal,
        backend=backend,
        runs=runs,
        artifacts=artifacts,
        spec=spec,
    )


@pytest.mark.asyncio
async def test_full_worker_uses_original_graph_and_recovers_artifact_after_service_restart(setup):
    s = setup
    task = s.app.prepare(s.context, s.proposal, proposal_key="prepare", source_run_id="run_source")
    assert s.backend.calls == 0
    snapshot = s.app.store.get(s.context.scope_key, task["task_id"])["snapshot"]
    assert snapshot["request"]["model_version"] == "1"
    assert snapshot["request"]["requested_by"] == "owner"
    assert snapshot["request"]["confirmed"] is False
    assert task["components"]["model_package"] == {"key": "h5-package", "version": "1"}
    assert task["source"]["run_id"] == "run_source"
    s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    assert s.backend.calls == 0
    assert await run_once(s.app, "independent-worker")
    assert not await run_once(s.app, "other-worker")
    assert s.backend.calls == 1
    restored = ModelTasks(
        TaskStore(s.app.store.path, workspace_root=s.app.store.path.parent),
        models=s.app.models,
        runs=s.runs,
        artifacts=s.artifacts,
    )
    result = await restored.get(s.context, task["task_id"])
    assert result["run"]["status"] == "succeeded"
    artifact_id = result["run"]["result_artifact_ids"][0]
    artifact = await restored.artifact(s.context, task["task_id"], artifact_id)
    assert artifact["analysis_result"]["blocks"]
    assert artifact["run_id"] == task["run_id"]
    assert s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])["state"] == "finished"
    assert s.backend.calls == 1
    record = s.artifacts.rows[artifact_id]
    s.artifacts.rows[artifact_id] = record.model_copy(update={"payload_sha256": "0" * 64})
    with pytest.raises(TaskConflict):
        await restored.artifact(s.context, task["task_id"], artifact_id)


@pytest.mark.asyncio
async def test_failed_execution_is_terminal_and_never_retried(setup):
    s = setup
    s.backend.fail = True
    task = s.app.prepare(s.context, s.proposal, proposal_key="fail")
    s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    await run_once(s.app, "worker")
    result = await s.app.get(s.context, task["task_id"])
    assert result["state"] == "finished" and result["run"]["status"] == "failed"
    assert "sensitive-private-path" not in str(result)
    assert not await run_once(s.app, "retry")
    assert s.backend.calls == 1


@pytest.mark.asyncio
async def test_unknown_requires_verified_terminal_receipt_before_release(setup):
    s = setup
    task = s.app.prepare(s.context, s.proposal, proposal_key="unknown")
    s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    row = s.app.store.claim("worker")
    s.app.store.finish(row["task_id"], "worker", unknown=True)
    with pytest.raises(TaskConflict):
        await reconcile(s.app, s.context, task["task_id"])
    assert s.app.store.claim("retry") is None
    assert s.backend.calls == 0


def test_identity_and_confirmed_fields_are_rejected(setup):
    for field, value in [("thread_id", "forged"), ("requested_by", "other"), ("confirmed", True)]:
        with pytest.raises(ValidationError):
            TaskProposal(**(setup.proposal.model_dump() | {field: value}))


def test_registration_change_requires_reprepare(setup):
    s = setup
    task = s.app.prepare(s.context, s.proposal, proposal_key="registration")
    s.app.models = ModelRegistry([s.spec.model_copy(update={"description": "changed"})])
    with pytest.raises(TaskConflict):
        s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    assert s.app.store.claim("worker") is None


@pytest_asyncio.fixture
async def api(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("h5-test-auth"))
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.SINGLE_USER)
    monkeypatch.setattr(settings, "ANALYSIS_SINGLE_USER_ID", "owner")
    registry = RunRegistry(tmp_path / "runtime.sqlite", workspace_root=tmp_path)
    tid = registry.create_thread("owner")["thread_id"]
    context = registry.require_thread("owner", tid)
    app = FastAPI()
    app.include_router(router)
    app.state.hermes_runtime = SimpleNamespace(registry=registry)
    app.state.model_tasks = setup.app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer h5-test-auth"},
    ) as client:
        yield client, app, tid, context, registry


@pytest.mark.asyncio
async def test_http_human_confirmation_and_cross_scope_boundaries(api, setup):
    client, app, tid, _context, registry = api
    base = f"/api/v1/hermes/threads/{tid}/model-tasks"
    payload = {"request_id": str(uuid4()), "proposal": setup.proposal.model_dump(mode="json")}
    response = await client.post(base, json=payload)
    assert response.status_code == 201
    task = response.json()
    url = base + "/" + task["task_id"]
    assert (await client.post(base, json=payload)).json()["task_id"] == task["task_id"]
    assert (await client.get(base)).json()["items"][0]["state"] == "pending"
    assert setup.backend.calls == 0
    assert (
        await client.post(
            url + "/confirm", json={"confirmed": False, "snapshot_sha256": task["snapshot_sha256"]}
        )
    ).status_code == 409
    assert (
        await client.post(url + "/confirm", json={"confirmed": True, "snapshot_sha256": "0" * 64})
    ).status_code == 409
    assert (
        await client.post(
            url + "/confirm",
            json={"confirmed": True, "snapshot_sha256": task["snapshot_sha256"], "parameters": {}},
        )
    ).status_code == 422
    other = registry.create_thread("owner")["thread_id"]
    assert (await client.get(url.replace(tid, other))).status_code == 404
    app.dependency_overrides[get_hermes_owner] = lambda: "other-owner"
    assert (await client.get(url)).status_code == 404
    app.dependency_overrides.clear()
    confirm = {"confirmed": True, "snapshot_sha256": task["snapshot_sha256"]}
    assert (await client.post(url + "/confirm", json=confirm)).status_code == 202
    assert (await client.post(url + "/confirm", json=confirm)).status_code == 202
    assert setup.backend.calls == 0
    await run_once(setup.app, "worker")
    result = (await client.get(url)).json()
    assert result["run"]["status"] == "succeeded"
    artifact_id = result["run"]["result_artifact_ids"][0]
    assert (await client.get(url + "/artifacts/" + artifact_id)).status_code == 200
    assert (
        await client.get(url.replace(tid, other) + "/artifacts/" + artifact_id)
    ).status_code == 404
    assert (await client.get(url, headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert setup.backend.calls == 1


@pytest.mark.asyncio
async def test_http_revoke_and_confirmation_replay(api, setup):
    client, _app, tid, _context, _registry = api
    base = f"/api/v1/hermes/threads/{tid}/model-tasks"
    task = (
        await client.post(
            base,
            json={"request_id": str(uuid4()), "proposal": setup.proposal.model_dump(mode="json")},
        )
    ).json()
    assert (await client.post(base + "/" + task["task_id"] + "/revoke", json={})).status_code == 200
    assert (
        await client.post(
            base + "/" + task["task_id"] + "/confirm",
            json={"confirmed": True, "snapshot_sha256": task["snapshot_sha256"]},
        )
    ).status_code == 409
    assert not await run_once(setup.app, "worker")
    assert setup.backend.calls == 0


@pytest.mark.asyncio
async def test_registration_changed_after_queue_does_not_start_model(setup):
    s = setup
    task = s.app.prepare(s.context, s.proposal, proposal_key="queued-registration")
    s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    s.app.models = ModelRegistry([s.spec.model_copy(update={"description": "changed"})])
    await run_once(s.app, "worker")
    result = await s.app.get(s.context, task["task_id"])
    assert result["state"] == "finished"
    assert result["run"]["error"]["code"] == "registration_changed_before_execution"
    assert s.backend.calls == 0
    assert s.app.store.claim("retry") is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("SCAT_H5_REAL_MONGO") != "1", reason="Explicit isolated Mongo acceptance only"
)
async def test_real_mongo_persistence_and_cleanup(setup, monkeypatch):
    from analysis_runs import AnalysisArtifactRepository, AnalysisRunRepository
    from scat.infrastructure.database import flight_database

    s = setup
    prefix = "h5_validation_" + uuid4().hex
    names = [prefix + "_runs", prefix + "_artifacts"]
    await flight_database.connect()
    existing_names = await flight_database.db.list_collection_names()
    assert not any(name in existing_names for name in names)
    runs = AnalysisRunRepository(collection=flight_database.get_collection(names[0]))
    artifacts = AnalysisArtifactRepository(collection=flight_database.get_collection(names[1]))
    s.app.runs = runs
    s.app.artifacts = artifacts
    monkeypatch.setattr(legacy, "analysis_run_repository", runs)
    monkeypatch.setattr(legacy, "analysis_artifact_repository", artifacts)
    try:
        task = s.app.prepare(s.context, s.proposal, proposal_key="real-mongo")
        s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
        await run_once(s.app, "mongo-worker")
        restored = ModelTasks(
            TaskStore(s.app.store.path, workspace_root=s.app.store.path.parent),
            models=s.app.models,
            runs=runs,
            artifacts=artifacts,
        )
        state = await restored.get(s.context, task["task_id"])
        assert state["state"] == "finished" and state["run"]["status"] == "succeeded"
        artifact_id = state["run"]["result_artifact_ids"][0]
        assert (await restored.artifact(s.context, task["task_id"], artifact_id))[
            "analysis_result"
        ]["blocks"]
        assert await runs.get_for_owner(task["run_id"], owner_user_id="other") is None
        assert await artifacts.get_for_owner(artifact_id, owner_user_id="other") is None
        assert not await run_once(restored, "second-worker")
        assert s.backend.calls == 1
    finally:
        for name in names:
            assert name.startswith(prefix + "_") and prefix.startswith("h5_validation_")
            await flight_database.db.drop_collection(name)
        remaining = await flight_database.db.list_collection_names()
        assert all(name not in remaining for name in names)
        await flight_database.close()


@pytest.mark.asyncio
async def test_final_persistence_loss_is_unknown_and_cannot_execute_twice(setup, monkeypatch):
    from pymongo.errors import ConnectionFailure

    s = setup
    original = s.runs.transition

    async def transition(run_id, **kwargs):
        if kwargs["status"] == ModelRunStatus.SUCCEEDED:
            raise ConnectionFailure("private-database-endpoint")
        await original(run_id, **kwargs)

    monkeypatch.setattr(s.runs, "transition", transition)
    task = s.app.prepare(s.context, s.proposal, proposal_key="receipt-loss")
    s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    await run_once(s.app, "worker")
    state = await s.app.get(s.context, task["task_id"])
    assert state["state"] == "unknown" and state["execution_outcome_unknown"]
    assert state["run"]["status"] == "running"
    assert s.backend.calls == 1
    assert not await run_once(s.app, "retry")
    assert s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])["state"] == "unknown"
    assert s.backend.calls == 1


@pytest.mark.asyncio
async def test_artifact_save_failure_is_terminal_without_model_retry(setup, monkeypatch):
    from pymongo.errors import ConnectionFailure

    s = setup

    async def fail(record):
        raise ConnectionFailure("private-database-endpoint")

    monkeypatch.setattr(s.artifacts, "create", fail)
    task = s.app.prepare(s.context, s.proposal, proposal_key="artifact-loss")
    s.app.confirm(s.context, task["task_id"], task["snapshot_sha256"])
    await run_once(s.app, "worker")
    state = await s.app.get(s.context, task["task_id"])
    assert state["state"] == "finished"
    assert state["run"]["error"]["code"] == "artifact_persistence_failed"
    assert state["run"]["result_artifact_ids"] == []
    assert not await run_once(s.app, "retry")
    assert s.backend.calls == 1
