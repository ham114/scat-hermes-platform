"""Container role boundaries, real MCP HTTP negotiation and unknown-state handling."""

import json

import pytest
from starlette.testclient import TestClient

from application.readonly import READ_ONLY_TOOLS, ReadOnlyApplication
from application.task_store import TaskConflict
from deployment_runtime.actions import allowed, create_app
from deployment_runtime.client import credential, private_origin


def payload(operation, **extra):
    return {
        "operation": operation,
        "arguments": {},
        "scope": "scat:" + "a" * 64,
        "source_run_id": "run_test",
        **extra,
    }


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:18123",
        "http://localhost:18123",
        "http://127.0.0.1:80",
        "http://user:password@127.0.0.1:18123",
        "http://127.0.0.1:18123/path",
        "http://127.0.0.1:18123?key=value",
    ],
)
def test_only_fixed_private_origins(value):
    with pytest.raises(ValueError):
        private_origin(value)


def test_credential_requires_project_private_file(tmp_path):
    (tmp_path / "var").mkdir()
    path = tmp_path / "var/key"
    path.write_text("a" * 48)
    path.chmod(0o600)
    assert credential(tmp_path, "var/key") == "a" * 48
    with pytest.raises(ValueError):
        credential(tmp_path, "../outside")


def test_main_cannot_supply_job_grant_or_execute_worker_effects():
    assert allowed("main", "workflows", payload("scat_workflow_catalog"))
    assert not allowed("main", "workflows", payload("scat_trial_run", job_session="invented"))
    assert not allowed("main", "tasks", payload("scat_task_catalog", job_session="invented"))
    assert not allowed(
        "main", "workflows", payload("scat_workflow_catalog", env={"PYTHONPATH": "/outside"})
    )
    assert not allowed("worker", "workflows", payload("scat_trial_run"))
    assert not allowed(
        "worker", "workflows", payload("scat_workflow_prepare", job_session="opaque")
    )
    assert allowed("worker", "steps", payload("scat_model_run", job_session="opaque"))


def test_real_mcp_http_auth_negotiation_and_tool_catalog():
    async def operation(**kwargs):
        return {"status": "ok", "data": kwargs}

    application = ReadOnlyApplication({name: operation for name in READ_ONLY_TOOLS})
    main_key, worker_key = "m" * 48, "w" * 48
    app = create_app({"main": main_key, "worker": worker_key}, application)
    with TestClient(app, base_url="http://127.0.0.1:18123") as client:
        assert client.get("/health").status_code == 200
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "h7-test", "version": "1"},
            },
        }
        assert client.post("/mcp/", json=body).status_code == 401
        assert (
            client.post(
                "/mcp/", json=body, headers={"Authorization": "Bearer " + worker_key}
            ).status_code
            == 401
        )
        headers = {
            "Authorization": "Bearer " + main_key,
            "Accept": "application/json, text/event-stream",
        }
        response = client.post("/mcp/", json=body, headers=headers)
        assert response.status_code == 200 and "result" in response.json()
        response = client.post(
            "/mcp/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert {item["name"] for item in response.json()["result"]["tools"]} == set(READ_ONLY_TOOLS)
        response = client.post(
            "/v1/actions/steps",
            json=payload("scat_model_run", job_session="forged"),
            headers={"Authorization": "Bearer " + main_key},
        )
        assert response.status_code == 403


def test_remote_birth_failure_is_unknown_not_false_process_death(monkeypatch):
    from application.skill_cpu_backend import process_start

    monkeypatch.setenv("SCAT_EXECUTION_REMOTE", "1")
    monkeypatch.setattr(
        "deployment_runtime.client.action_request", lambda *args, **kwargs: {"status": "unknown"}
    )
    with pytest.raises(TaskConflict, match="unknown"):
        process_start(123)


def test_private_task_bridge_uses_bound_context_without_subprocess(tmp_path, monkeypatch):
    from hermes_runtime.task_bridge import TaskToolBridge

    captured = []
    monkeypatch.setattr(
        "deployment_runtime.client.action_request",
        lambda root, env, namespace, value, **kwargs: (
            captured.append((namespace, value)) or {"status": "ok"}
        ),
    )
    bridge = TaskToolBridge(
        "/no/host/python",
        tmp_path / "scripts/run_model_task_tool.py",
        env={"SCAT_ACTION_BASE_URL": "http://127.0.0.1:18123"},
    )
    bridge.bind("native", "trusted-scope", "trusted-run")
    result = json.loads(bridge.handler("scat_task_catalog")({}, session_id="native"))
    assert result["status"] == "ok"
    assert captured[0][0] == "tasks" and captured[0][1]["scope"] == "trusted-scope"
    assert captured[0][1]["source_run_id"] == "trusted-run"


def test_job_controller_rejects_model_paths_and_unauthenticated_requests(tmp_path, monkeypatch):
    from deployment_runtime import jobs

    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setenv("HERMES_TASK_STATE_FILE", "var/state.sqlite")
    with TestClient(jobs.create_app("k" * 48)) as client:
        assert client.post("/v1/jobs/start", json={}).status_code == 401
        value = {"task_id": "../escape", "worker": "worker", "generation": 1, "delegation": "grant"}
        assert (
            client.post(
                "/v1/jobs/start", headers={"Authorization": "Bearer " + "k" * 48}, json=value
            ).status_code
            == 400
        )
