"""One HTTP acceptance covers validation, approval, queue, real worker and stored artifacts."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from application.skill_job_factory import create_skill_tasks
from application.skill_worker import SkillWorker
from hermes_runtime.registry import RunRegistry
from service.auth import verify_bearer
from service.hermes_api import get_hermes_owner
from service.model_task_api import router as task_router
from service.workflow_api import router as workflow_router
from tests.hermes_runtime.test_skill_jobs_s4 import Artifacts, Runs
from tests.workflows.test_approved_workflows import execute_flow

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_http_forecast_validates_queues_executes_and_restores_without_duplicate_compute(
    tmp_path,
):
    registry = RunRegistry(tmp_path / "threads.sqlite", workspace_root=ROOT)
    thread = registry.create_thread("scat-researcher")["thread_id"]
    tasks = create_skill_tasks(
        ROOT,
        tmp_path / "jobs.sqlite",
        runs=Runs(),
        artifacts=Artifacts(),
        workflows_enabled=True,
        workflow_synthetic=True,
    )
    app = FastAPI()
    app.include_router(workflow_router)
    app.include_router(task_router)
    app.state.hermes_runtime = SimpleNamespace(registry=registry)
    app.state.model_tasks = tasks
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "scat-researcher"
    base = f"/api/v1/hermes/threads/{thread}"
    proposal = {
        "kind": "forecast",
        "external_ids": ["SYNTH-S5-001"],
        "models": [{"model": "constant-velocity-heading"}],
        "observation_last_n": 20,
        "horizons_s": [10, 30, 60],
    }
    frozen_id = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for invalid in [True, "20", 2, 2001]:
                response = await client.post(
                    base + "/workflow-tasks",
                    json={
                        "request_id": str(uuid4()),
                        "proposal": {**proposal, "observation_last_n": invalid},
                    },
                )
                assert response.status_code == 422
            insufficient = await client.post(
                base + "/workflow-tasks",
                json={
                    "request_id": str(uuid4()),
                    "proposal": {**proposal, "observation_last_n": 38},
                },
            )
            assert insufficient.status_code == 409
            with tasks.store.connect() as db:
                assert db.execute("SELECT count(*) FROM model_tasks").fetchone()[0] == 0
            payload = {"request_id": str(uuid4()), "proposal": proposal}
            response = await client.post(base + "/workflow-tasks", json=payload)
            assert response.status_code == 201
            task = response.json()
            frozen_id = task["workflow"]["input_sha256"]
            assert task["state"] == "pending" and task["input_refs"][0]["limit"] == 20
            assert tasks.skills.store.claim("before-confirmation") is None
            assert (await client.post(base + "/workflow-tasks", json=payload)).json()[
                "task_id"
            ] == task["task_id"]
            endpoint = base + "/model-tasks/" + task["task_id"]
            for body in [
                {"confirmed": False, "snapshot_sha256": task["snapshot_sha256"]},
                {"confirmed": True, "snapshot_sha256": "0" * 64},
            ]:
                assert (await client.post(endpoint + "/confirm", json=body)).status_code == 409
            body = {"confirmed": True, "snapshot_sha256": task["snapshot_sha256"]}
            assert (await client.post(endpoint + "/confirm", json=body)).status_code == 202
            assert (await client.post(endpoint + "/confirm", json=body)).status_code == 202
            assert (await client.get(endpoint)).json()["skill_job"]["state"] == "queued"
            with tasks.store.connect() as db:
                assert db.execute("SELECT count(*) FROM skill_jobs").fetchone()[0] == 1
                assert (
                    db.execute("SELECT count(*) FROM skill_backend_executions").fetchone()[0] == 0
                )
            assert await SkillWorker(tasks, execute_flow).once()
            final = (await client.get(endpoint)).json()
            assert final["skill_job"]["state"] == final["run"]["status"] == "succeeded"
            assert len(final["skill_job"]["backend_executions"]) == 1
            assert final["skill_job"]["backend_executions"][0]["calls"] == 1
            ids = final["run"]["result_artifact_ids"]
            assert len(ids) == 2
            hashes = []
            for identity in ids:
                data = (await client.get(endpoint + "/artifacts/" + identity)).json()
                hashes.append(data["payload_sha256"])
                assert data["analysis_result"]["context"]["prediction_mode"] == "future_forecast"
                assert not any(
                    c["key"].startswith(("mae_", "rmse_", "ade_", "fde_"))
                    for block in data["analysis_result"]["blocks"]
                    for c in block.get("columns", [])
                )
            reopened = create_skill_tasks(
                ROOT,
                tasks.store.path,
                runs=tasks.runs,
                artifacts=tasks.artifacts,
                workflows_enabled=True,
                workflow_synthetic=True,
            )
            app.state.model_tasks = reopened
            for identity, sha in zip(ids, hashes, strict=True):
                assert (await client.get(endpoint + "/artifacts/" + identity)).json()[
                    "payload_sha256"
                ] == sha
            assert not await SkillWorker(reopened, execute_flow).once()
            assert (await client.get(endpoint)).json()["skill_job"]["backend_executions"][0][
                "calls"
            ] == 1
            other = registry.create_thread("scat-researcher")["thread_id"]
            assert (await client.get(endpoint.replace(thread, other))).status_code == 404
    finally:
        if frozen_id:
            (ROOT / "var/workflow-inputs" / (frozen_id + ".json")).unlink(missing_ok=True)
