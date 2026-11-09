from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

from application.context import ExecutionContext
from core import settings
from core.settings import AnalysisIdentityMode
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.registry import RequestConflictError, RunRegistry, ThreadBusyError
from hermes_runtime.results import ToolResultStore
from service.errors import install_exception_handlers
from service.hermes_api import get_hermes_owner, router

BASE = "/api/v1/hermes"


class Native:
    def __init__(self):
        self.requests = []
        self.runs = {}
        self.keys = {}
        self.lose_receipt = False
        self.frames = None
        self.reject = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/runs":
            if self.reject:
                return httpx.Response(self.reject, json={"error": "PRIVATE-REJECTION"})
            key = request.headers["Idempotency-Key"]
            body = json.loads(request.content)
            if key in self.keys:
                rid, old = self.keys[key]
                if old != body:
                    return httpx.Response(409, json={"error": "private-upstream-detail"})
                return httpx.Response(
                    202, json={"run_id": rid, "status": self.runs[rid]["status"], "replayed": True}
                )
            rid = "run_" + uuid4().hex
            self.keys[key] = rid, body
            self.runs[rid] = {"run_id": rid, "status": "running", "session_id": "PRIVATE-SESSION"}
            if self.lose_receipt:
                self.lose_receipt = False
                raise httpx.ReadTimeout("private-upstream-detail", request=request)
            return httpx.Response(202, json={"run_id": rid, "status": "started"})
        rid = path.split("/")[3]
        if path.endswith("/stop"):
            self.runs[rid].update(status="cancelled", completed=False, interrupted=True)
        if path.endswith("/events"):
            events = self.frames or [
                {"event": "reasoning.available", "text": "PRIVATE-REASONING"},
                {
                    "event": "tool.started",
                    "tool": "mcp__scat__search_scat_flights",
                    "preview": "PRIVATE-PREVIEW",
                },
                {"event": "message.delta", "delta": "正常中文"},
                {
                    "event": "run.completed",
                    "output": "OK",
                    "completed": True,
                    "session_id": "PRIVATE-SESSION",
                },
            ]
            frames = "".join(
                "id: "
                + str(i)
                + "\ndata: "
                + json.dumps(event | {"run_id": rid, "seq": i}, ensure_ascii=False)
                + "\n\n"
                for i, event in enumerate(events)
            )
            return httpx.Response(
                200, content=frames, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=self.runs[rid] | {"error": "PRIVATE-ERROR"})


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("platform-test-secret"))
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.SINGLE_USER)
    monkeypatch.setattr(settings, "ANALYSIS_SINGLE_USER_ID", "owner-one")
    native = Native()
    registry = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    runtime = HermesHTTPRuntime(
        "http://127.0.0.1:18081",
        SecretStr("native-test-secret-1234"),
        registry,
        transport=httpx.MockTransport(native),
        results=ToolResultStore(tmp_path / "results.sqlite", workspace_root=tmp_path),
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    app.state.hermes_runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://platform",
        headers={"Authorization": "Bearer platform-test-secret"},
    ) as client:
        yield client, native, registry, app
    await runtime.aclose()


async def thread(client):
    response = await client.post(BASE + "/threads", json={})
    assert response.status_code == 201
    return response.json()["thread_id"]


async def run(client, tid, *, request_id=None, message="Hello"):
    return await client.post(
        f"{BASE}/threads/{tid}/runs",
        json={"request_id": str(request_id or uuid4()), "message": message},
    )


@pytest.mark.asyncio
async def test_auth_and_server_generated_identity(api):
    client, native, registry, app = api
    assert (
        await client.post(BASE + "/threads", json={}, headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    tid = await thread(client)
    assert registry.require_thread("owner-one", tid)
    assert (
        await client.get(BASE + "/threads", headers={"X-Authenticated-User": "attacker"})
    ).json()["items"][0]["thread_id"] == tid
    app.dependency_overrides[get_hermes_owner] = lambda: "owner-two"
    assert (await client.get(BASE + "/threads")).json() == {"items": [], "next_cursor": None}
    for path in [f"/threads/{tid}/runs", f"/threads/{tid}/runs/run_foreign/events"]:
        assert (await client.get(BASE + path)).status_code == 404
    assert native.requests == []


@pytest.mark.asyncio
async def test_create_poll_scope_status_and_cancel(api):
    client, native, registry, _ = api
    tid = await thread(client)
    response = await run(client, tid)
    assert response.status_code == 202
    rid = response.json()["run_id"]
    path = f"{BASE}/threads/{tid}/runs/{rid}"
    assert "PRIVATE" not in (await client.get(path)).text
    context = ExecutionContext("owner-one", tid)
    assert native.requests[0].headers["X-Hermes-Session-Key"] == context.scope_key
    assert registry.active_run(context) == rid
    assert (await run(client, tid)).status_code == 409
    stopped = await client.post(path + "/stop", json={})
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    assert stopped.json()["completed"] is False
    assert registry.active_run(context) is None
    assert (await run(client, tid)).status_code == 202


@pytest.mark.asyncio
async def test_unknown_admission_requires_same_uuid_payload_and_survives_reopen(api, tmp_path):
    client, native, _registry, _ = api
    tid = await thread(client)
    request_id = uuid4()
    native.lose_receipt = True
    response = await run(client, tid, request_id=request_id)
    assert response.status_code == 503 and response.json()["error"]["outcome_unknown"] is True
    assert "private-upstream" not in response.text
    reloaded = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    with pytest.raises(ThreadBusyError):
        reloaded.reserve_request(ExecutionContext("owner-one", tid), str(uuid4()), "different")
    assert (await run(client, tid)).status_code == 409
    assert (await run(client, tid, request_id=request_id, message="changed")).status_code == 409
    recovered = await run(client, tid, request_id=request_id)
    assert recovered.status_code == 202 and recovered.json()["replayed"] is True
    assert len(native.runs) == 1
    assert len([r for r in native.requests if r.method == "POST"]) == 2


@pytest.mark.asyncio
async def test_sse_filters_private_fields_supports_cursor_and_releases_on_terminal(api):
    client, native, registry, _ = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    response = await client.get(
        f"{BASE}/threads/{tid}/runs/{rid}/events", headers={"Last-Event-ID": "1"}
    )
    assert response.status_code == 200
    assert response.headers["x-accel-buffering"] == "no"
    assert "正常中文" in response.text and '"run.completed"' in response.text
    assert "PRIVATE" not in response.text and "tool.started" not in response.text
    assert native.requests[-1].headers["Last-Event-ID"] == "1"
    assert registry.active_run(ExecutionContext("owner-one", tid)) is None
    assert (await run(client, tid)).status_code == 202


@pytest.mark.asyncio
async def test_sse_disconnect_is_error_and_keeps_active_admission(api):
    client, native, registry, _ = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    native.frames = [{"event": "message.delta", "delta": "partial"}]
    response = await client.get(f"{BASE}/threads/{tid}/runs/{rid}/events")
    assert '"stream.error"' in response.text and "event_stream_interrupted" in response.text
    assert '"run.completed"' not in response.text
    assert registry.active_run(ExecutionContext("owner-one", tid)) == rid
    assert (await run(client, tid)).status_code == 409


@pytest.mark.asyncio
async def test_foreign_run_missing_thread_and_input_identity_refused_before_http(api):
    client, native, _, _app = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    other = await thread(client)
    before = len(native.requests)
    assert (await client.get(f"{BASE}/threads/{other}/runs/{rid}")).status_code == 404
    assert (
        await client.post(f"{BASE}/threads/{other}/runs/{rid}/stop", json={})
    ).status_code == 404
    response = await client.post(
        f"{BASE}/threads/{tid}/runs",
        json={"request_id": str(uuid4()), "message": "Hello", "user_id": "PRIVATE-INPUT"},
    )
    assert response.status_code == 422 and "PRIVATE-INPUT" not in response.text
    assert len(native.requests) == before
    for cursor in ["bad", "-1", str(2**64)]:
        assert (
            await client.get(
                f"{BASE}/threads/{tid}/runs/{rid}/events", headers={"Last-Event-ID": cursor}
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_finished_unwatched_run_allows_next_and_changed_idempotent_payload_refused(api):
    client, native, _, _ = api
    tid = await thread(client)
    request_id = uuid4()
    rid = (await run(client, tid, request_id=request_id)).json()["run_id"]
    native.runs[rid]["status"] = "completed"
    assert (await run(client, tid, request_id=request_id, message="changed")).status_code == 409
    assert (await run(client, tid)).status_code == 202
    history = (await client.get(f"{BASE}/threads/{tid}/runs")).json()["items"]
    assert len(history) == 2 and history[1]["request_id"] == str(request_id)


def test_atomic_admission_across_connections_and_thread_isolation(tmp_path):
    registry = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    tid = registry.create_thread("owner")["thread_id"]
    context = registry.require_thread("owner", tid)

    def reserve(_):
        instance = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
        try:
            instance.reserve_request(context, str(uuid4()), "same")
            return True
        except ThreadBusyError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(16))) == 1
    other = ExecutionContext("owner", registry.create_thread("owner")["thread_id"])
    registry.reserve_request(other, "same-id", "one")
    with pytest.raises(RequestConflictError):
        registry.reserve_request(other, "same-id", "two")


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [400, 401, 403, 409, 422, 429])
async def test_definite_rejection_frees_pending_slot_without_leaking_error(api, upstream_status):
    client, native, _registry, _ = api
    tid = await thread(client)
    native.reject = upstream_status
    response = await run(client, tid)
    assert response.status_code >= 400 and "PRIVATE" not in response.text
    assert response.json()["error"]["outcome_unknown"] is False
    native.reject = None
    assert (await run(client, tid)).status_code == 202


@pytest.mark.asyncio
async def test_upstream_500_keeps_ambiguous_admission_blocked(api):
    client, native, _, _ = api
    tid = await thread(client)
    native.reject = 500
    response = await run(client, tid)
    assert response.status_code == 503 and response.json()["error"]["outcome_unknown"] is True
    assert (await run(client, tid)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [" ", "x" * 32001])
async def test_message_boundary_rejected_before_upstream(api, message):
    client, native, *_ = api
    tid = await thread(client)
    response = await run(client, tid, message=message)
    assert response.status_code == 422
    assert native.requests == []


@pytest.mark.asyncio
async def test_message_at_maximum_is_accepted_without_truncation(api):
    client, native, *_ = api
    tid = await thread(client)
    message = "长" * 32000
    response = await run(client, tid, message=message)
    assert response.status_code == 202
    assert json.loads(native.requests[0].content)["input"] == message


@pytest.mark.asyncio
async def test_result_receipts_are_owner_thread_run_scoped_and_survive_native_failure(api):
    client, native, _registry, app = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    context = ExecutionContext("owner-one", tid)
    ref = app.state.hermes_runtime.results.put(
        context.scope_key,
        rid,
        "call-1",
        "search_scat_flights",
        {"status": "ok", "data": {"total": 10637}},
    )
    path = f"{BASE}/threads/{tid}/runs/{rid}"
    assert (await client.get(path + "/results")).json()["items"] == [ref]
    assert (await client.get(path)).json()["result_references"] == [ref]
    native.runs.clear()  # Receipt reads have no dependency on the upstream process.
    assert (await client.get(path + "/results/" + ref["result_id"])).json()["result"]["data"][
        "total"
    ] == 10637
    other = await thread(client)
    assert (await client.get(f"{BASE}/threads/{other}/runs/{rid}/results")).status_code == 404
    app.dependency_overrides[get_hermes_owner] = lambda: "owner-two"
    assert (await client.get(path + "/results/" + ref["result_id"])).status_code == 404


@pytest.mark.asyncio
async def test_track_display_is_built_from_authorized_receipt_without_upstream_calls(api):
    client, native, _, app = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    raw = {
        "status": "ok",
        "query": {"external_id": "SYNTH"},
        "data": {
            "pagination": {"start": 0, "total_points": 2, "has_more": False},
            "points": [
                {"timestamp": "2020-01-01T00:00:00Z", "position": {"lat_deg": 50, "lon_deg": 4}},
                {"timestamp": "2020-01-01T00:00:01Z", "position": {"lat_deg": 51, "lon_deg": 5}},
            ],
        },
    }
    ref = app.state.hermes_runtime.results.put(
        ExecutionContext("owner-one", tid).scope_key,
        rid,
        "track-call",
        "get_scat_track_points",
        raw,
    )
    path = f"{BASE}/threads/{tid}/runs/{rid}/results/{ref['result_id']}"
    before = len(native.requests)
    body = (await client.get(path)).json()
    assert body["result"] == raw
    assert body["payload_sha256"] == ref["payload_sha256"]
    assert body["analysis_result"]["context"]["readonly_result_id"] == ref["result_id"]
    assert any(b["type"] == "trajectory_2d" for b in body["analysis_result"]["blocks"])
    assert (await client.get(path)).json() == body
    assert len(native.requests) == before
    app.dependency_overrides[get_hermes_owner] = lambda: "owner-two"
    assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_result_events_resolve_only_authoritative_scoped_receipts(api):
    client, native, _, app = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    ref = app.state.hermes_runtime.results.put(
        ExecutionContext("owner-one", tid).scope_key,
        rid,
        "call-1",
        "search_scat_flights",
        {"status": "ok", "data": {"total": 2}},
    )
    native.frames = [
        {"event": "business.result", "result_id": "f" * 32, "operation": "PRIVATE-FAKE"},
        {
            "event": "business.result",
            "result_id": ref["result_id"],
            "operation": "PRIVATE-OVERRIDE",
            "preview": "PRIVATE-PREVIEW",
        },
        {"event": "run.completed", "completed": True, "output": "OK"},
    ]
    response = await client.get(f"{BASE}/threads/{tid}/runs/{rid}/events")
    assert response.text.count('"event": "business.result"') == 1
    assert ref["result_id"] in response.text and "PRIVATE" not in response.text
    assert '"operation": "search_scat_flights"' in response.text


@pytest.mark.asyncio
async def test_stop_reports_conversation_scope_and_leaves_business_task_untouched(api):
    client, native, _, app = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]
    # An already-submitted executor-owned task is independent of the dialogue request.
    app.state.existing_business_task = {"status": "running", "job_id": "existing-task"}
    before = len(native.requests)
    result = (await client.post(f"{BASE}/threads/{tid}/runs/{rid}/stop", json={})).json()
    assert result["status"] == "cancelled" and result["completed"] is False
    assert result["cancellation_scope"] == "conversation_only"
    assert result["business_tasks_cancelled"] is False
    assert app.state.existing_business_task["status"] == "running"
    assert [x.url.path for x in native.requests[before:]] == [f"/v1/runs/{rid}/stop"]


@pytest.mark.asyncio
async def test_stop_acknowledgement_is_not_terminal_or_business_job_cancellation(api, monkeypatch):
    client, _native, registry, app = api
    tid = await thread(client)
    rid = (await run(client, tid)).json()["run_id"]

    async def stopping(context, run_id):
        assert run_id == rid
        return {"run_id": rid, "status": "stopping"}

    monkeypatch.setattr(app.state.hermes_runtime, "stop_run", stopping)
    response = (await client.post(f"{BASE}/threads/{tid}/runs/{rid}/stop", json={})).json()
    assert response["status"] == "stopping" and response["business_tasks_cancelled"] is False
    assert response["cancellation_scope"] == "conversation_only"
    assert registry.active_run(ExecutionContext("owner-one", tid)) == rid
    assert (await run(client, tid)).status_code == 409


@pytest.mark.asyncio
async def test_trajectory_view_is_owner_scoped_cached_and_expiry_checked(api, monkeypatch):
    from time import time
    from types import SimpleNamespace

    client, native, registry, app = api
    tid = (await client.post(BASE + "/threads", json={})).json()["thread_id"]
    rid = (
        await client.post(
            f"{BASE}/threads/{tid}/runs", json={"request_id": str(uuid4()), "message": "绘制轨迹"}
        )
    ).json()["run_id"]
    context = registry.require_thread("owner-one", tid)
    store = app.state.hermes_runtime.results
    raw = {
        "status": "ok",
        "query": {"batch": "SYNTH", "external_id": "100002", "start": 0, "every_nth": 1},
        "data": {
            "pagination": {
                "start": 0,
                "total_points": 3,
                "returned_points": 3,
                "source_points_consumed": 3,
                "has_more": False,
                "next_start": None,
            },
            "points": [
                {"sequence": i, "position": {"lat_deg": 50 + i / 100, "lon_deg": 4 + i / 100}}
                for i in range(3)
            ],
        },
    }
    ref = store.put(context.scope_key, rid, "seed", "get_scat_track_points", raw)
    calls = []

    async def invoke(name, args, *, context):
        calls.append(args)
        return raw

    app.state.hermes_runtime.trajectory_view_application = SimpleNamespace(invoke=invoke)
    route = f"{BASE}/threads/{tid}/runs/{rid}/results/{ref['result_id']}/trajectory"
    response = await client.get(route)
    assert response.status_code == 200 and response.json()["coverage"]["complete"]
    assert (await client.get(route)).json() == response.json()
    assert len(calls) == 1
    listed = (await client.get(f"{BASE}/threads/{tid}/runs/{rid}/results")).json()["items"]
    assert all(r["trajectory_view_available"] for r in listed)
    app.dependency_overrides[get_hermes_owner] = lambda: "other-owner"
    assert (await client.get(route)).status_code == 404
    app.dependency_overrides.clear()
    later = time() + 90000
    monkeypatch.setattr("hermes_runtime.results.time.time", lambda: later)
    assert (await client.get(route)).status_code == 404
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_history_corrects_false_full_coverage_without_rewriting_original_journal(api):
    client, native, registry, app = api
    tid = (await client.post(BASE + "/threads", json={})).json()["thread_id"]
    rid = (
        await client.post(
            f"{BASE}/threads/{tid}/runs", json={"request_id": str(uuid4()), "message": "分析轨迹"}
        )
    ).json()["run_id"]
    context = registry.require_thread("owner-one", tid)
    text = "我读取了全部524个点。\n\n趋势是向北。"
    registry.save_turn(
        context, {"run_id": rid, "status": "completed", "output": text, "completed": True}
    )
    raw = {
        "status": "ok",
        "query": {"batch": "SYNTH", "external_id": "100002", "start": 0, "every_nth": 1},
        "data": {
            "pagination": {"total_points": 524},
            "points": [{"sequence": i} for i in range(3)],
        },
    }
    app.state.hermes_runtime.results.put(
        context.scope_key, rid, "seed", "get_scat_track_points", raw
    )
    response = await client.get(f"{BASE}/threads/{tid}/messages")
    assert response.status_code == 200
    output = response.json()["items"][0]["output"]
    assert "3/524" in output and "读取了全部" not in output
    assert registry.history_turns(context, 20, None)["items"][0]["output"] == text
