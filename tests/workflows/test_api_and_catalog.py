"""Workflow HTTP approval, machine schema and role admission boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from application.skill_job_factory import create_skill_tasks
from hermes_runtime.registry import RunRegistry
from hermes_runtime.skill_guard import SkillOperationGuard
from hermes_runtime.skills import NativeSkillBridge, SkillRelease
from service.auth import verify_bearer
from service.hermes_api import get_hermes_owner
from service.model_task_api import router as tasks_router
from service.workflow_api import router
from tests.hermes_runtime.test_skill_jobs_s4 import Artifacts, Runs
from workflow_runtime.catalog import CONTROLS, OPERATIONS, schemas

ROOT = Path(__file__).resolve().parents[2]


def test_reviewed_schema_exactly_matches_code_and_has_no_confirmation_or_shell():
    reviewed = json.loads(
        (ROOT / "services/hermes/workflow-operations.v1.json").read_text(encoding="utf-8")
    )
    assert reviewed["controls"] == schemas(controls=True)
    assert [
        {k: r[k] for k in ("name", "description", "parameters")} for r in reviewed["operations"]
    ] == schemas()
    assert all(r["toolset"] == OPERATIONS[r["name"]][0] for r in reviewed["operations"])
    for row in schemas() + schemas(controls=True):
        properties = row["parameters"].get("properties", {})
        assert not set(properties) & {
            "confirmed",
            "scope",
            "job_session",
            "script",
            "code",
            "python",
            "worker",
            "generation",
        }
    assert len(OPERATIONS) == 8 and len(CONTROLS) == 5


def test_legacy_mapping_has_exactly_every_original_tool_and_explicit_gpu_boundary():
    old = json.loads(
        (ROOT / "docs/validation/20261023-skills-reuse-inventory.json").read_text(encoding="utf-8")
    )["legacy_tool_definitions"]
    mapping = json.loads(
        (ROOT / "services/hermes/legacy-tool-mapping.v1.json").read_text(encoding="utf-8")
    )
    expected = {(path, name) for path, names in old.items() for name in names}
    actual = {(r["legacy_module"], r["legacy_tool"]) for r in mapping["items"]}
    assert actual == expected and len(mapping["items"]) == 52
    assert mapping["production_gpu"] is True
    assert "有限批准任务" in mapping["production_gpu_scope"]
    assert mapping["native_readonly_tools"] == 28
    assert all(
        r["target"]
        and r["scope_note"]
        and r["status"]
        in {
            "native_readonly",
            "native_control",
            "scoped_merge",
            "http_control",
            "retained_http",
            "disabled_production_gpu",
        }
        for r in mapping["items"]
    )


@pytest.mark.asyncio
async def test_http_prepare_never_confirms_and_controls_preserve_scope(tmp_path):
    registry = RunRegistry(tmp_path / "threads.sqlite", workspace_root=ROOT)
    tid = registry.create_thread("scat-researcher")["thread_id"]
    app = FastAPI()
    app.include_router(router)
    app.include_router(tasks_router)
    tasks = create_skill_tasks(
        ROOT,
        tmp_path / "jobs.sqlite",
        runs=Runs(),
        artifacts=Artifacts(),
        workflows_enabled=True,
        workflow_synthetic=True,
    )
    app.state.model_tasks = tasks
    app.state.hermes_runtime = SimpleNamespace(registry=registry)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "scat-researcher"
    path = f"/api/v1/hermes/threads/{tid}"
    identity = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            catalog = await client.get("/api/v1/hermes/workflow-catalog")
            assert catalog.json()["enabled"] and catalog.json()["synthetic"]
            payload = {
                "request_id": str(uuid4()),
                "proposal": {
                    "kind": "comparison",
                    "external_ids": ["SYNTH-S5-001"],
                    "models": [{"model": "kalman-cv"}],
                },
            }
            response = await client.post(path + "/workflow-tasks", json=payload)
            assert response.status_code == 201, response.text
            view = response.json()
            identity = view["workflow"]["input_sha256"]
            assert view["confirmation_required"] and tasks.skills.store.claim("premature") is None
            assert (await client.post(path + "/workflow-tasks", json=payload)).json()[
                "task_id"
            ] == view["task_id"]
            endpoint = path + "/model-tasks/" + view["task_id"]
            body = {"confirmed": False, "snapshot_sha256": view["snapshot_sha256"]}
            assert (await client.post(endpoint + "/confirm", json=body)).status_code == 409
            body["confirmed"] = True
            assert (await client.post(endpoint + "/confirm", json=body)).status_code == 202
            other = registry.create_thread("scat-researcher")["thread_id"]
            assert (await client.get(endpoint.replace(tid, other))).status_code == 404
            assert (await client.post(endpoint + "/cancel", json={})).json()["skill_job"][
                "state"
            ] == "cancelled"
            assert tasks.skills.store.claim("after-cancel") is None
    finally:
        if identity:
            (ROOT / "var/workflow-inputs" / (identity + ".json")).unlink(missing_ok=True)


def test_prepare_requires_the_matching_primary_skill_not_any_loaded_workflow(tmp_path):
    release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
    # Stage under the real workspace because published release containment is intentional.
    home = ROOT / ".tmp/s5" / ("guard-" + uuid4().hex)
    try:
        folder = release.stage(home)
        native = SimpleNamespace(
            skill_search_dirs=lambda: ([(1, folder)], folder),
            skill_view=lambda name, file_path, **kw: json.dumps(
                {
                    "success": True,
                    "content": (folder / name / (file_path or "SKILL.md")).read_text(
                        encoding="utf-8"
                    ),
                }
            ),
        )
        bridge = NativeSkillBridge(release, folder, native, home / "trace.sqlite")
        bridge.bind("native", "scope", "run")
        guard = SkillOperationGuard(bridge, {"scat_workflow_prepare"}, set())

        def dispatch(*a, **kw):
            return json.dumps({"status": "ok"})

        bridge.handler("skill_view")({"name": "scat-report"}, session_id="native")
        args = {"proposal": {"kind": "experiment"}}
        assert (
            json.loads(guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="native"))[
                "status"
            ]
            == "forbidden"
        )
        bridge.handler("skill_view")({"name": "scat-experiment"}, session_id="native")
        assert (
            json.loads(guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="native"))[
                "status"
            ]
            == "ok"
        )
    finally:
        import shutil

        assert home.resolve().parent == (ROOT / ".tmp/s5").resolve() and not home.is_symlink()
        if home.exists():
            shutil.rmtree(home)
