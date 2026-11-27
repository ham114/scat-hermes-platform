"""Conversation delivery, authorization, immutable revisions and shared budgets."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from hermes_runtime.skill_budget import RunBudget
from hermes_runtime.skill_policy import Budget
from hermes_runtime.visualization_bridge import VisualizationBridge
from schema.analysis import AnalysisResult, VisualizationRefBlock
from service.auth import verify_bearer
from service.hermes_api import get_hermes_owner, get_runtime
from service.visualization_api import router
from tests.visualization_runtime.test_pipeline import pipeline as _pipeline
from visualization_runtime.contracts import CreateVisualization, ReviseVisualization
from visualization_runtime.ledger import ExecutionLedger
from visualization_runtime.providers import Completion
from visualization_runtime.store import JobConflict

pipeline = _pipeline


def parent(app, context, *, calls=30, tokens=300000):
    path = app.jobs.path.parent / "parent.sqlite"
    RunBudget(
        path,
        context.scope_key,
        "run_fixture",
        Budget(max_turns=calls, max_llm_tokens=tokens),
        "frozen",
    )
    app.parent_budget_path = path
    return path


def published(pipeline):
    app, context, job, worker, *_ = pipeline
    asyncio.run(worker.run_once())
    return app.view(context, job["task_id"]).artifact_id


def test_feedback_creates_linked_version_and_preserves_old_artifact(pipeline):
    app, context, job, worker, calls, *_ = pipeline
    artifact = published(pipeline)
    parent(app, context)
    old = app.publisher.read_file(context, artifact, "desktop.png")
    request = ReviseVisualization(request_id="feedback-1", feedback="改成蓝色，增大字号")
    second = app.revise(context, artifact, request, parent_run_id="run_fixture")
    assert (
        app.revise(context, artifact, request, parent_run_id="run_fixture")["task_id"]
        == second["task_id"]
    )
    assert second["request"].snapshot_id == job["request"].snapshot_id
    assert app.publisher.read_file(context, artifact, "desktop.png") == old
    assert asyncio.run(worker.run_once())
    view = app.view(context, second["task_id"])
    assert view.state == "succeeded" and view.version == 2 and view.parent_artifact_id == artifact
    new = app.artifact_view(context, view.artifact_id)
    assert new.version == 2 and new.parent_artifact_id == artifact
    assert new.analysis_result.schema_version == "1.7"
    assert calls == {"code": 2, "review": 2}
    assert app.artifact_view(context, artifact).version == 1
    assert calls == {"code": 2, "review": 2}  # History calls no model.


def test_parent_tokens_and_calls_shared_idempotently_and_atomically(pipeline):
    app, context, job, *_ = pipeline
    path = parent(app, context, calls=2, tokens=60000)
    request = job["request"]

    def admit(index):
        try:
            return app.jobs.create(
                context, "parallel-" + str(index), request, parent=(path, "run_fixture")
            )
        except JobConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(admit, range(2)))
    admitted = next(r for r in rows if r)
    assert sum(r is not None for r in rows) == 1
    assert (
        app.jobs.create(context, admitted["request_id"], request, parent=(path, "run_fixture"))[
            "task_id"
        ]
        == admitted["task_id"]
    )
    # Remove the preexisting maintenance job so the admitted child claims next.
    app.jobs.cancel(context, job["task_id"])
    claimed = app.jobs.claim("worker")
    ledger = ExecutionLedger(app.jobs)
    for index in range(2):
        row = ledger.reserve_call(
            claimed["task_id"],
            "worker",
            claimed["generation"],
            call_key="call-" + str(index),
            role="code",
            fingerprint=str(index) * 64,
            tokens=100,
        )
        assert (
            ledger.reserve_call(
                claimed["task_id"],
                "worker",
                claimed["generation"],
                call_key="call-" + str(index),
                role="code",
                fingerprint=str(index) * 64,
                tokens=100,
            )["call_id"]
            == row["call_id"]
        )
    with pytest.raises(JobConflict, match="parent_budget_exceeded"):
        ledger.reserve_call(
            claimed["task_id"],
            "worker",
            claimed["generation"],
            call_key="third",
            role="review",
            fingerprint="f" * 64,
            tokens=100,
        )
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT charged_tokens,llm_calls,provider_input_tokens FROM skill_run_budgets"
        ).fetchone() == (60000, 2, 0)
    app.jobs.cancel(context, claimed["task_id"])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT charged_tokens FROM skill_run_budgets").fetchone()[0] == 60000


def test_api_scopes_files_revision_cancel_disabled_and_revocation(pipeline):
    app, original, _job, worker, calls, *_ = pipeline
    artifact = published(pipeline)
    parent(app, original)
    thread = str(uuid4())

    def require_thread(owner, tid):
        if owner != original.user_id or tid != thread:
            from hermes_runtime.registry import ThreadNotFoundError

            raise ThreadNotFoundError("missing")
        return original

    runtime = SimpleNamespace(
        registry=SimpleNamespace(
            require_thread=require_thread,
            require=lambda *args: None,
            list_runs=lambda *args: [{"run_id": "run_fixture"}],
        ),
        results=app.receipts,
    )
    web = FastAPI()
    web.include_router(router)
    web.state.visualization_tasks = app
    web.dependency_overrides[verify_bearer] = lambda: None
    web.dependency_overrides[get_hermes_owner] = lambda: original.user_id
    web.dependency_overrides[get_runtime] = lambda: runtime
    prefix = f"/api/v1/hermes/threads/{thread}"

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://test"
        ) as client:
            manifest = (await client.get(prefix + f"/visualizations/{artifact}")).json()
            assert (
                manifest["version"] == 1
                and manifest["analysis_result"]["blocks"][0]["type"] == "visualization_ref"
            )
            image = await client.get(prefix + f"/visualizations/{artifact}/files/desktop.png")
            assert image.status_code == 200 and image.headers["cache-control"] == "no-store"
            code = await client.get(prefix + f"/visualizations/{artifact}/files/source.js")
            assert (
                code.headers["content-type"].startswith("text/plain")
                and "attachment" in code.headers["content-disposition"]
            )
            assert (
                await client.get(prefix + f"/visualizations/{artifact}/files/.env")
            ).status_code == 404
            refs = manifest["data_refs"]
            body = {"request_id": "api-create", "intent": "绘制本页", "data_refs": refs}
            created = await client.post(prefix + "/visualizations", json=body)
            assert created.status_code == 202
            assert (await client.post(prefix + "/visualizations", json=body)).json()[
                "task_id"
            ] == created.json()["task_id"]
            assert (
                await client.post(prefix + "/visualizations", json=body | {"owner": "attacker"})
            ).status_code == 422
            assert (
                await client.post(prefix + "/visualizations", json=body | {"intent": "different"})
            ).status_code == 409
            cancelled = await client.post(
                prefix + f"/visualization-tasks/{created.json()['task_id']}/cancel"
            )
            assert cancelled.json()["state"] == "cancelled"
            response = await client.post(
                prefix + f"/visualizations/{artifact}/revisions",
                json={"request_id": "revision", "feedback": "增大字号"},
            )
            assert response.status_code == 202
            assert await worker.run_once()
            revision = (
                await client.get(prefix + f"/visualization-tasks/{response.json()['task_id']}")
            ).json()
            assert revision["version"] == 2 and revision["state"] == "succeeded"
            assert (
                revision["artifact_id"] != artifact and revision["parent_artifact_id"] == artifact
            )
            app.enabled = False
            assert (
                await client.post(
                    prefix + "/visualizations", json=body | {"request_id": "disabled"}
                )
            ).status_code == 409
            assert (await client.get(prefix + f"/visualizations/{artifact}")).status_code == 200
            web.dependency_overrides[get_hermes_owner] = lambda: "attacker"
            assert (await client.get(prefix + f"/visualizations/{artifact}")).status_code == 404
            web.dependency_overrides[get_hermes_owner] = lambda: original.user_id
            assert (
                await client.post(prefix + "/visualization-sources/revoke", json=refs[0])
            ).status_code == 200
            assert (
                await client.get(prefix + f"/visualizations/{artifact}/files/desktop.png")
            ).status_code == 404
            assert (
                await client.delete(
                    prefix + f"/runs/{refs[0]['run_id']}/results/{refs[0]['source_id']}"
                )
            ).status_code == 200
            with pytest.raises(LookupError):
                app.receipts.get(original.scope_key, refs[0]["run_id"], refs[0]["source_id"])
            assert (
                await client.post(prefix + "/visualizations", json=body | {"request_id": "revoked"})
            ).status_code == 409  # disabled

    asyncio.run(exercise())
    assert calls == {"code": 2, "review": 2}


def test_bridge_cannot_forge_owner_paths_or_approval(monkeypatch):
    from deployment_runtime.actions import allowed

    bridge = VisualizationBridge("python", "private", env={})
    seen = []
    monkeypatch.setattr(bridge, "_invoke", lambda payload: seen.append(payload) or {"status": "ok"})
    invoke = bridge.handler("create_visualization")
    assert json.loads(invoke({}, session_id="unknown"))["status"] == "forbidden"
    bridge.bind("native", "trusted-scope", "run_trusted")
    invoke({"request_id": "stable", "owner": "attacker"}, session_id="native")
    assert seen[0]["scope"] == "trusted-scope" and seen[0]["source_run_id"] == "run_trusted"
    assert allowed("main", "visualizations", seen[0])
    assert not allowed("worker", "visualizations", seen[0])
    assert not allowed("main", "visualizations", seen[0] | {"owner": "attacker"})
    with pytest.raises(ValueError):
        CreateVisualization.model_validate({"owner": "attacker"})
    with pytest.raises(ValueError):
        bridge.handler("render")


def test_reference_block_requires_new_version():
    block = VisualizationRefBlock(title="图表", artifact_id="a" * 32, thread_id="thread", version=2)
    with pytest.raises(ValueError, match="1.7"):
        AnalysisResult(
            schema_version="1.6", dataset="scat", operation="plot", title="图", blocks=[block]
        )


def test_provider_overrun_is_preserved_in_parent_budget(pipeline):
    app, context, job, *_ = pipeline
    path = parent(app, context)
    row = app.jobs.create(context, "overrun", job["request"], parent=(path, "run_fixture"))
    app.jobs.cancel(context, job["task_id"])
    claimed = app.jobs.claim("worker")
    ledger = ExecutionLedger(app.jobs)
    call = ledger.reserve_call(
        row["task_id"],
        "worker",
        claimed["generation"],
        call_key="paid",
        role="code",
        fingerprint="a" * 64,
        tokens=100,
    )
    ledger.start_call(row["task_id"], "worker", claimed["generation"], call["call_id"])
    ledger.finish_call(
        row["task_id"],
        "worker",
        claimed["generation"],
        call["call_id"],
        completion=Completion(
            content="source",
            model_matches=True,
            request_id="actual",
            usage={"prompt_tokens": 60000, "completion_tokens": 1000, "total_tokens": 61000},
        ),
    )
    assert app.view(context, row["task_id"]).state == "needs_reconciliation"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT charged_tokens,exhausted FROM skill_run_budgets").fetchone() == (
            61000,
            "visualization_provider_overrun",
        )


def test_model_artifact_sources_keep_their_business_run_identity(pipeline):
    from visualization_runtime.snapshots import digest

    app, context, *_ = pipeline
    parent(app, context)
    result = AnalysisResult(
        dataset="scat",
        operation="metric",
        title="模型结果",
        blocks=[
            {
                "type": "metrics",
                "title": "指标",
                "items": [{"key": "rmse", "label": "RMSE", "value": 1.2, "unit": "m"}],
            }
        ],
    )
    record = SimpleNamespace(
        artifact_id="model-artifact",
        owner_user_id=context.user_id,
        thread_id=context.thread_id,
        run_id="f" * 32,
        expires_at=None,
        analysis_result=result,
        payload_sha256=digest(result.model_dump(mode="json")),
    )

    async def listing(**kwargs):
        return [
            record,
            SimpleNamespace(
                **(vars(record) | {"thread_id": "another", "artifact_id": "foreign-artifact"})
            ),
        ]

    async def get(identity, **kwargs):
        return record if identity == record.artifact_id else None

    app.artifacts = SimpleNamespace(list_for_owner=listing, get_for_owner=get)
    registry = SimpleNamespace(list_runs=lambda *args: [{"run_id": "run_fixture"}])
    sources = asyncio.run(app.sources(context, registry))["items"]
    model = next(r for r in sources if r["kind"] == "analysis_artifact")
    assert model["run_id"] == record.run_id and not any(
        r["source_id"] == "foreign-artifact" for r in sources
    )
    body = CreateVisualization(
        request_id="model-source",
        intent="绘制模型误差",
        data_refs=[{k: v for k, v in model.items() if k != "operation"}],
    )
    job = asyncio.run(app.create(context, body, parent_run_id="run_fixture"))
    assert (
        app.require_snapshot(context, job["task_id"])["payload"]["datasets"][0]["reference"][
            "run_id"
        ]
        == record.run_id
    )


def test_history_read_does_not_require_provider_credentials(pipeline):
    from application.visualization_tasks import create_visualization_tasks
    from visualization_runtime.settings import ConfigurationError, VisualizationSettings

    app, context, job, *_ = pipeline
    artifact = published(pipeline)
    path = parent(app, context)
    reader = create_visualization_tasks(
        app.jobs.workspace_root,
        app.jobs.path,
        VisualizationSettings(enabled=True, artifact_root="artifacts"),
        app.receipts,
        parent_budget_path=path,
        read_only=True,
    )
    assert reader.publisher.read_file(context, artifact, "desktop.png")
    refs = app.require_snapshot(context, job["task_id"])["payload"]["datasets"]
    body = CreateVisualization(
        request_id="no-credentials", intent="draw", data_refs=[refs[0]["reference"]]
    )
    with pytest.raises(ConfigurationError, match="missing_configuration"):
        asyncio.run(reader.create(context, body, parent_run_id="run_fixture"))
