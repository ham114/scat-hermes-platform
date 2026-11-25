import json

import httpx
import pytest
from fastapi import FastAPI

from hermes_runtime.diagnostics import DiagnosticStore, LangfuseReader, summary
from service import diagnostics_api
from service.auth import verify_bearer
from service.hermes_api import get_hermes_owner


def test_owner_binding_pagination_and_business_failure_are_preserved(tmp_path):
    store = DiagnosticStore(tmp_path / "trace.sqlite", root=tmp_path)
    first = store.bind("scope-a", "run-a", session_id="native-a")
    assert store.bind("scope-a", "run-a", owner="alice", thread_id="thread-a") == first
    with pytest.raises(ValueError, match="binding_conflict"):
        store.bind("scope-a", "run-a", owner="bob")
    second = store.bind("scope-b", "run-b", owner="bob")
    with pytest.raises(LookupError):
        store.get(first, "bob")
    assert store.get(first, "admin", admin=True)["owner"] == "alice"
    assert store.list("alice")[0][0]["id"] == first
    rows, more = store.list("admin", admin=True, limit=1)
    assert more and rows[0]["id"] == second
    assert (
        store.list("admin", admin=True, limit=1, before=(rows[0]["created"], second))[0][0]["id"]
        == first
    )
    store.node(
        "scope-a",
        "run-a",
        "call-a",
        "查询",
        state="failed",
        started=1,
        output={"status": "invalid_request", "password": "PRIVATE", "data": None},
    )
    store.finish("scope-a", "run-a", "failed")
    store.finish("scope-a", "run-a", "completed")
    assert store.get(first, "alice")["state"] == "failed"
    assert "PRIVATE" not in json.dumps(store.nodes(first))
    assert store.nodes(first)[0]["output"]["status"] == "invalid_request"


def test_summary_does_not_capture_tool_content():
    value = summary(
        json.dumps(
            {
                "status": "ok",
                "data": {
                    "items": [{"secret": "PRIVATE"}],
                    "pagination": {"has_more": True, "next_start": 10},
                    "text": "PRIVATE",
                },
            }
        )
    )
    assert value["counts"] == {"items": 1}
    assert value["pagination"]["next_start"] == 10
    assert "PRIVATE" not in json.dumps(value)


@pytest.mark.asyncio
async def test_reader_rejects_wrong_session_and_marks_backend_failure(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200, json={"data": [{"id": "private-trace", "sessionId": "someone-else"}]}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    reader = LangfuseReader(
        {"base_url": "http://test", "public_key": "public", "secret_key": "private"}
    )
    assert await reader.observations("my-session") == ([], "pending")
    assert len(seen) == 1
    assert "/v2/observations?sessionId=my-session" in seen[0]
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(503))
        ),
    )
    assert await reader.observations("my-session") == ([], "unavailable")


@pytest.mark.asyncio
async def test_api_checks_owner_before_contacting_langfuse(tmp_path, monkeypatch):
    store = DiagnosticStore(tmp_path / "trace.sqlite", root=tmp_path)
    identity = store.bind("scope", "run-a", owner="alice", session_id="native")
    monkeypatch.setattr(diagnostics_api, "journal", lambda: store)
    monkeypatch.setattr(diagnostics_api, "configuration", dict)
    called = []

    async def observations(self, session):
        called.append(session)
        return [], "unavailable"

    monkeypatch.setattr(LangfuseReader, "observations", observations)
    app = FastAPI()
    app.include_router(diagnostics_api.router)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "bob"
    monkeypatch.setattr(diagnostics_api, "access", lambda request, owner: (owner, False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/hermes/diagnostics/" + identity)).status_code == 404
        assert not called
        app.dependency_overrides[get_hermes_owner] = lambda: "alice"
        response = await client.get("/api/v1/hermes/diagnostics/" + identity)
        assert response.status_code == 200 and response.json()["langfuse_status"] == "unavailable"
        assert "session_id" not in response.json()["trace"]
        assert called == ["native"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{"a": 1, "b": 2}, [float("inf"), "a" * 32]])
async def test_api_rejects_structurally_invalid_cursor(monkeypatch, value):
    import base64

    app = FastAPI()
    app.include_router(diagnostics_api.router)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "alice"
    monkeypatch.setattr(diagnostics_api, "journal", lambda: pytest.fail("must validate first"))
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/api/v1/hermes/diagnostics", params={"cursor": cursor})
        ).status_code == 422


@pytest.mark.asyncio
async def test_render_event_rejects_a_receipt_from_another_trace(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from application.context import ExecutionContext
    from hermes_runtime.results import ToolResultStore

    context = ExecutionContext("alice", "thread-a")
    store = DiagnosticStore(tmp_path / "diagnostic.sqlite", root=tmp_path)
    trace = store.bind(context.scope_key, "run_a", owner="alice")
    receipts = ToolResultStore(tmp_path / "results.sqlite", workspace_root=tmp_path)
    reference = receipts.put(
        context.scope_key,
        "run_other",
        "call",
        "get_scat_track_points",
        {"status": "ok", "data": {"points": []}},
    )
    monkeypatch.setattr(diagnostics_api, "journal", lambda: store)
    app = FastAPI()
    app.include_router(diagnostics_api.router)
    app.state.hermes_runtime = SimpleNamespace(results=receipts)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "alice"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/hermes/diagnostics/" + trace + "/render",
            json={
                "result_id": reference["result_id"],
                "payload_sha256": reference["payload_sha256"],
                "event_id": "11111111-1111-4111-8111-111111111111",
                "state": "rendered",
            },
        )
        assert response.status_code == 404
        assert not store.nodes(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", [False, True])
async def test_historical_task_checks_verified_snapshot_owner(tmp_path, monkeypatch, workflow):
    from types import SimpleNamespace

    from application.context import ExecutionContext
    from application.task_store import TaskStore

    context = ExecutionContext("alice", "thread-a")
    tasks = TaskStore(tmp_path / "tasks.sqlite", workspace_root=tmp_path)
    snapshot = {
        "proposal": {},
        "run": {"run_id": "analysis-a", "owner_user_id": "alice"},
        "source": {"thread_id": context.thread_id, "run_id": "source-a"},
    }
    if workflow:
        snapshot["workflow"] = {}
    else:
        snapshot["request"] = {"requested_by": "alice"}
    row = tasks.prepare(context.scope_key, "proposal-a", snapshot)
    reads = []

    async def get(context, task_id):
        reads.append(context.user_id)
        return {"run": {"status": "completed"}}

    app = FastAPI()
    app.include_router(diagnostics_api.router)
    app.state.model_tasks = SimpleNamespace(store=tasks, skills=None, get=get)
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: "bob"
    journal = DiagnosticStore(tmp_path / "diagnostics.sqlite", root=tmp_path)
    monkeypatch.setattr(diagnostics_api, "journal", lambda: journal)
    monkeypatch.setattr(diagnostics_api, "configuration", lambda: None)
    monkeypatch.setattr(diagnostics_api, "access", lambda request, owner: (owner, False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        path = "/api/v1/hermes/diagnostics/tasks/" + row["task_id"]
        assert (await client.get(path)).status_code == 404
        assert not reads
        app.dependency_overrides[get_hermes_owner] = lambda: "alice"
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["trace"]["owner"] == "alice"
        assert response.json()["langfuse_status"] == "pending"
        assert reads == ["alice"]
