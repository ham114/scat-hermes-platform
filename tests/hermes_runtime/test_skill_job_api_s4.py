"""Human control API remains separate from every native business tool capability."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from application.skill_job_factory import create_skill_tasks
from hermes_runtime.registry import RunRegistry
from service.auth import verify_bearer
from service.hermes_api import get_hermes_owner
from service.model_task_api import router
from tests.hermes_runtime.test_skill_jobs_s4 import Artifacts, Runs, prepare

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_http_approval_cancel_and_owner_isolation(tmp_path):
    registry = RunRegistry(tmp_path / "threads.sqlite", workspace_root=ROOT)
    tid = registry.create_thread("scat-researcher")["thread_id"]
    context = registry.require_thread("scat-researcher", tid)
    tasks = create_skill_tasks(
        ROOT, tmp_path / "jobs.sqlite", cpu_fixture=True, runs=Runs(), artifacts=Artifacts()
    )
    _, view = prepare(tasks, context=context)
    app = FastAPI()
    app.include_router(router)
    app.state.model_tasks = tasks
    app.state.hermes_runtime = SimpleNamespace(registry=registry)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "scat-researcher"
    path = f"/api/v1/hermes/threads/{tid}/model-tasks/{view['task_id']}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                path + "/confirm",
                json={"snapshot_sha256": view["snapshot_sha256"], "confirmed": False},
            )
        ).status_code == 409
        approved = await client.post(
            path + "/confirm", json={"snapshot_sha256": view["snapshot_sha256"], "confirmed": True}
        )
        assert approved.status_code == 202 and approved.json()["skill_job"]["approved"]
        other = registry.create_thread("scat-researcher")["thread_id"]
        assert (await client.get(path.replace(tid, other))).status_code == 404
        cancelled = await client.post(path + "/cancel", json={})
        assert (
            cancelled.status_code == 200 and cancelled.json()["skill_job"]["state"] == "cancelled"
        )
        assert (await client.post(path + "/cancel", json={"confirmed": True})).status_code == 422
    assert tasks.skills.store.claim("worker") is None


@pytest.mark.asyncio
async def test_http_budget_revision_requires_explicit_confirmation_and_snapshot(tmp_path):
    registry = RunRegistry(tmp_path / "threads.sqlite", workspace_root=ROOT)
    tid = registry.create_thread("scat-researcher")["thread_id"]
    context = registry.require_thread("scat-researcher", tid)
    tasks = create_skill_tasks(
        ROOT, tmp_path / "jobs.sqlite", cpu_fixture=True, runs=Runs(), artifacts=Artifacts()
    )
    _, view = prepare(tasks, context=context, budget={"max_tool_calls": 2})
    tasks.confirm(context, view["task_id"], view["snapshot_sha256"])
    tasks.skills.store.state(context.scope_key, view["task_id"], "paused")
    app = FastAPI()
    app.include_router(router)
    app.state.model_tasks = tasks
    app.state.hermes_runtime = SimpleNamespace(registry=registry)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "scat-researcher"
    path = f"/api/v1/hermes/threads/{tid}/model-tasks/{view['task_id']}/budget"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "snapshot_sha256": view["snapshot_sha256"],
            "confirmed": False,
            "limits": {"max_tool_calls": 4},
        }
        assert (await client.post(path, json=body)).status_code == 409
        body["confirmed"] = True
        body["snapshot_sha256"] = "0" * 64
        assert (await client.post(path, json=body)).status_code == 409
        body["snapshot_sha256"] = view["snapshot_sha256"]
        result = await client.post(path, json=body)
        assert result.status_code == 200 and result.json()["skill_job"]["budget_revision"] == 1
        limits = tasks.skills.store.limits(view["task_id"])
        assert (
            limits["max_tool_calls"] == 4
            and limits["max_llm_tokens"] == view["skill_job"]["budget"]["max_llm_tokens"]
        )
