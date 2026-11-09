import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from application.context import ExecutionContext
from hermes_runtime.author_bridge import AuthorBridge
from hermes_runtime.autonomy_config import AutonomyPolicy, LearningPolicy, MemoryPolicy
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.daily_budget import DailyBudget
from hermes_runtime.experience_learning import (
    LearningLedger,
    collect_readonly,
    sha,
    verify_automatic_content,
)
from hermes_runtime.honcho_memory import HonchoClient, MemoryRuntime, preference
from hermes_runtime.registry import RunRegistry
from hermes_runtime.results import ToolResultStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_memory_ui_records_deletion_and_epoch_isolation(memory):
    identity = uuid4()
    memory.correct("owner", "请记住：使用公里", identity)
    rows = memory.corrections("owner")["items"]
    assert rows[0]["request_id"] == str(identity)
    assert rows[0]["state"] == "pending"
    assert memory.corrections("other")["items"] == []
    await memory.drain()
    assert memory.corrections("owner")["items"][0]["state"] == "sent"
    assert memory.status("owner")["deletion"] == "none"
    memory.forget("owner")
    assert memory.corrections("owner")["items"] == []
    assert memory.status("owner")["deletion"] == "pending"
    await memory.drain()
    assert memory.status("owner")["deletion"] == "completed"
    assert not memory.status("owner")["enabled"]


class MemoryClient:
    def __init__(self):
        self.value, self.writes, self.deletes = "偏好中文、公里", [], []
        self.fail = False
        self.http = httpx.AsyncClient()

    async def recall(self, workspace, query=""):
        if self.fail:
            raise httpx.ConnectError("offline")
        return self.value

    async def write(self, workspace, session, identity, content):
        self.writes.append((workspace, session, identity, content))
        if self.fail:
            raise httpx.ReadTimeout("outcome unknown")

    async def delete(self, workspace):
        self.deletes.append(workspace)
        return True


@pytest.fixture
def memory(tmp_path):
    key = tmp_path / "identity.key"
    key.write_text("i" * 48)
    key.chmod(0o600)
    return MemoryRuntime(
        tmp_path,
        MemoryPolicy(state="memory.sqlite", identity_key="identity.key"),
        client=MemoryClient(),
    )


def test_config_strict_and_bounded():
    with pytest.raises(ValidationError):
        AutonomyPolicy.model_validate({"learning": {"auto_publish": True}})
    with pytest.raises(ValidationError):
        LearningPolicy(min_distinct_inputs=0)
    with pytest.raises(ValidationError):
        MemoryPolicy(enabled="true")


@pytest.mark.parametrize("text", ["请记住：我喜欢中文", "I prefer concise answers", "以后请用公里"])
def test_explicit_preferences(text):
    assert preference(text) == text


@pytest.mark.parametrize(
    "text",
    ["工具返回收入123", "请记住 api_key=private123", "请记住密码：abc", "请记住 " + "a" * 4100],
)
def test_nonpreference_or_secret_not_recorded(text):
    assert preference(text) is None


@pytest.mark.asyncio
async def test_memory_original_input_fixed_snapshot_and_queue(memory):
    context = ExecutionContext("owner", "thread")
    identity, original = uuid4(), "请记住：我喜欢简短中文"
    registry = RunRegistry(memory.root / "registry.sqlite", workspace_root=memory.root)
    requests = []

    def native(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"run_id": "run_one", "status": "started"})

    async with HermesHTTPRuntime(
        "http://127.0.0.1:18121",
        SecretStr("k" * 32),
        registry,
        memory=memory,
        transport=httpx.MockTransport(native),
    ) as runtime:
        await runtime.create_run(context, original, request_id=identity)
        memory.client.value = "changed recall"
        await runtime.create_run(context, original, request_id=identity)
    assert requests[0] == requests[1]
    assert requests[0]["input"] == original and "偏好中文" in requests[0]["instructions"]
    await memory.drain()
    await memory.drain()
    assert len(memory.client.writes) == 1
    assert memory.status("other")["queue"] == {}
    assert memory.client.writes[0][0] != memory.workspace(memory.owner("other"), 1)
    await memory.aclose()


@pytest.mark.asyncio
async def test_unadmitted_request_does_not_write_and_changed_input_conflicts(memory):
    ctx, request = ExecutionContext("owner", "thread"), uuid4()
    await memory.prepare(ctx, "请记住：用公里", request)
    await memory.drain()
    assert memory.client.writes == []
    with pytest.raises(ValueError, match="conflict"):
        await memory.prepare(ctx, "请记住：用英里", request)
    await memory.aclose()


@pytest.mark.asyncio
async def test_unknown_write_not_retried_after_restart(memory):
    ctx, request = ExecutionContext("owner", "thread"), uuid4()
    await memory.prepare(ctx, "请记住：用公里", request)
    memory.admitted(ctx, request)
    memory.client.fail = True
    await memory.drain()
    await memory.drain()
    restarted = MemoryRuntime(memory.root, memory.policy, client=memory.client)
    await restarted.drain()
    assert len(memory.client.writes) == 1
    assert memory.status("owner")["queue"]["unknown"] == 1
    await memory.aclose()


@pytest.mark.asyncio
async def test_correction_idempotency_forget_revokes_and_rotates(memory):
    ctx, request = ExecutionContext("owner", "thread"), uuid4()
    await memory.prepare(ctx, "问题", request)
    correction = uuid4()
    assert not memory.correct("owner", "更正：单位公里", correction)["replayed"]
    assert memory.correct("owner", "更正：单位公里", correction)["replayed"]
    with pytest.raises(ValueError, match="conflict"):
        memory.correct("owner", "单位英里", correction)
    assert "更正：单位公里" in await memory.prepare(ctx, "下一个问题", uuid4())
    assert memory.forget("owner")["deletion"] == "pending"
    await memory.drain()
    assert len(memory.client.deletes) == 1 and memory.client.writes == []
    with pytest.raises(ValueError, match="revoked"):
        await memory.prepare(ctx, "问题", request)
    assert memory.status("owner")["enabled"] is False
    assert await memory.prepare(ctx, "新问题", uuid4()) == ""
    memory.settings("owner", True)
    assert memory.status("owner")["epoch"] == 2
    await memory.aclose()


@pytest.mark.asyncio
async def test_memory_unavailable_pins_empty_snapshot(memory):
    ctx, request = ExecutionContext("owner", "thread"), uuid4()
    memory.client.fail = True
    initial = await memory.prepare(ctx, "问题", request)
    assert "本次召回暂不可用" in initial
    memory.client.fail = False
    assert await memory.prepare(ctx, "问题", request) == initial
    await memory.aclose()


@pytest.mark.asyncio
async def test_disabled_cancels_pending(memory):
    ctx, request = ExecutionContext("owner", "thread"), uuid4()
    await memory.prepare(ctx, "请记住：用公里", request)
    memory.admitted(ctx, request)
    memory.settings("owner", False)
    await memory.drain()
    assert memory.client.writes == []
    await memory.aclose()


@pytest.mark.asyncio
async def test_honcho_protocol_and_scoped_jwt():
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(201, json={})

    client = HonchoClient(
        "http://127.0.0.1:18140", "signing", transport=httpx.MockTransport(endpoint)
    )
    await client.write("workspace-a", "session-a", "event", "preference")
    assert requests[0].url.path == "/v3/workspaces"
    assert requests[-1].url.path == "/v3/workspaces/workspace-a/sessions/session-a/messages"
    assert json.loads(requests[-1].content)["messages"][0]["metadata"]["scat_event_id"] == "event"
    assert requests[0].headers["authorization"] != requests[-1].headers["authorization"]
    await client.http.aclose()


@pytest.mark.asyncio
async def test_honcho_delete_sessions_before_workspace_and_confirm_real_resource():
    paths = []
    deleted = [False]

    def endpoint(request):
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("/sessions/list"):
            return httpx.Response(200, json={"items": [{"id": "s"}], "total": 1})
        if request.url.path.endswith("/workspaces/list"):
            return httpx.Response(
                200, json={"items": [] if deleted[0] else [{"id": "w"}], "pages": 1}
            )
        if request.method == "DELETE" and request.url.path == "/v3/workspaces/w":
            deleted[0] = True
        return httpx.Response(202, json={})

    client = HonchoClient(
        "http://127.0.0.1:18140", "signing", transport=httpx.MockTransport(endpoint)
    )
    assert await client.delete("w")
    assert paths == [
        ("POST", "/v3/workspaces/list"),
        ("POST", "/v3/workspaces/w/sessions/list"),
        ("DELETE", "/v3/workspaces/w/sessions/s"),
        ("DELETE", "/v3/workspaces/w"),
        ("POST", "/v3/workspaces/list"),
    ]
    await client.http.aclose()


def test_daily_budget_concurrency_restart_failed_charged_and_rollover(tmp_path):
    now = [1700000000]
    budget = DailyBudget(
        tmp_path / "budget.sqlite",
        daily_requests=3,
        daily_units=1000,
        lifetime_requests=4,
        clock=lambda: now[0],
    )

    def reserve(_):
        try:
            identity = budget.reserve("author", b"request", 100)
            budget.finish(identity, "unknown")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 3
    restarted = DailyBudget(
        budget.path, daily_requests=3, daily_units=1000, lifetime_requests=4, clock=lambda: now[0]
    )
    with pytest.raises(ValueError, match="exhausted"):
        restarted.reserve("memory", b"r", 1)
    now[0] += 86400
    restarted.reserve("trial", b"r", 1)
    with pytest.raises(ValueError, match="exhausted"):
        restarted.reserve("trial", b"r", 1)


def record(ledger, identity, value):
    ledger.record_verified(
        identity=identity,
        owner="owner",
        operations=["mcp__scat__search_scat_flights"],
        input_sha=sha(value),
        evidence_sha=sha(identity),
    )


def test_learning_threshold_dedup_concurrency_daily_and_restart(tmp_path):
    policy = LearningPolicy(
        state="learning.sqlite",
        min_successes=3,
        min_distinct_inputs=2,
        max_drafts_per_day=1,
        cooldown_hours=0,
    )
    ledger = LearningLedger(tmp_path, policy)
    for identity in ("a", "b", "c"):
        record(ledger, identity, "same-input")
    assert ledger.reserve() is None
    record(ledger, "c", "same-input")
    record(ledger, "d", "different-input")
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [job for job in pool.map(lambda _: ledger.reserve(), range(4)) if job]
    assert len(jobs) == 1
    ledger.update(jobs[0]["id"], "ready_for_review")
    for identity in ("e", "f", "g"):
        record(ledger, identity, identity)
    assert LearningLedger(tmp_path, policy).reserve() is None
    assert ledger.status("other")["jobs"] == []
    assert not ledger.status("owner")["automatic_publication"]


def test_learning_unknown_blocks_dispatch(tmp_path):
    ledger = LearningLedger(
        tmp_path,
        LearningPolicy(
            state="learning.sqlite", min_successes=1, min_distinct_inputs=1, cooldown_hours=0
        ),
    )
    record(ledger, "a", "a")
    job = ledger.reserve()
    ledger.update(job["id"], "unknown")
    record(ledger, "b", "b")
    assert ledger.reserve() is None


def test_explicit_retry_keeps_failure_and_charges_new_daily_slot(tmp_path):
    ledger = LearningLedger(
        tmp_path,
        LearningPolicy(
            state="learning.sqlite", min_successes=1, min_distinct_inputs=1, max_drafts_per_day=2
        ),
    )
    record(ledger, "a", "a")
    first = ledger.reserve()
    ledger.update(first["id"], "unknown")
    with pytest.raises(ValueError, match="known_failure"):
        ledger.retry_failed(first["id"])
    ledger.update(first["id"], "failed")
    second = ledger.retry_failed(first["id"])
    assert second["id"] != first["id"] and second["evidence"] == first["evidence"]
    assert ledger.get(first["id"])["state"] == "failed"
    ledger.update(second["id"], "failed")
    with pytest.raises(ValueError, match="already_created"):
        ledger.retry_failed(first["id"])
    with pytest.raises(ValueError, match="daily_learning"):
        ledger.retry_failed(second["id"])


def test_automatic_author_cannot_access_other_candidate_or_publish(tmp_path):
    path = tmp_path / "author.key"
    path.write_text("k" * 48)
    path.chmod(0o600)
    candidate, draft = "a" * 32, "b" * 32
    bridge = AuthorBridge(
        tmp_path,
        "http://127.0.0.1:18124",
        "author.key",
        automatic_candidate=candidate,
        automatic_scope="allowed-scope",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"draft": draft})),
    )
    with pytest.raises(ValueError, match="scope_rejected"):
        bridge.bind("s", "other-scope", "run")
    bridge.bind("s", "allowed-scope", "run")
    invoke = bridge.handler("skill_draft")
    for op, identity in [
        ("learning_read", "c" * 32),
        ("update", "c" * 32),
        ("create", None),
        ("trial", draft),
        ("status", draft),
        ("learning_list", None),
        ("publish", None),
    ]:
        assert not json.loads(invoke({"operation": op, "identity": identity}, session_id="s"))[
            "success"
        ]
    assert json.loads(invoke({"operation": "read", "identity": draft}, session_id="s")) == {
        "draft": draft
    }
    bridge.client.close()


def test_automatic_content_cannot_expand_observed_operations_or_modify_published():
    candidate = {
        "evidence": [{"summary": json.dumps({"operations": ["mcp__scat__search_scat_flights"]})}]
    }
    change = {"name": "auto-example", "allowed_operations": ["mcp__scat__search_scat_flights"]}
    verify_automatic_content(candidate, {"changes": [change]})
    with pytest.raises(ValueError, match="verified_operations"):
        verify_automatic_content(candidate, {"changes": [change | {"name": "scat-flight-query"}]})
    with pytest.raises(ValueError, match="verified_operations"):
        verify_automatic_content(
            candidate,
            {
                "changes": [
                    change | {"allowed_operations": ["mcp__scat__get_scat_database_overview"]}
                ]
            },
        )


@pytest.mark.asyncio
async def test_collector_requires_terminal_owner_trace_and_untampered_receipt(tmp_path):
    key = tmp_path / "native.key"
    key.write_text("k" * 48)
    key.chmod(0o600)
    policy = LearningPolicy(
        state="learning.sqlite",
        registry="runs.sqlite",
        results="results.sqlite",
        traces="traces.sqlite",
        native_key="native.key",
        owner="owner",
    )
    ledger = LearningLedger(tmp_path, policy)
    registry = RunRegistry(tmp_path / policy.registry, workspace_root=tmp_path)
    thread = registry.create_thread("owner")["thread_id"]
    context = ExecutionContext("owner", thread)
    registry.bind(context, str(uuid4()), "run_one")
    results = ToolResultStore(tmp_path / policy.results, workspace_root=tmp_path)
    results.put(
        context.scope_key,
        "run_one",
        "call",
        "get_scat_database_overview",
        {"status": "ok", "data": {"count": 1}},
    )
    with sqlite3.connect(tmp_path / policy.traces) as db:
        db.execute(
            "CREATE TABLE skill_operations(id INTEGER, scope TEXT,run_id TEXT,operation TEXT,arguments_sha256 TEXT,outcome TEXT,result_sha256 TEXT,bundle_sha256 TEXT)"
        )
        db.execute(
            "INSERT INTO skill_operations VALUES(1,?,?,?,?,?,?,?)",
            (
                context.scope_key,
                "run_one",
                "mcp__scat__get_scat_database_overview",
                sha({}),
                "returned",
                sha("result"),
                sha("bundle"),
            ),
        )
    state = {"run_id": "run_one", "status": "running"}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=state))
    assert await collect_readonly(tmp_path, policy, ledger, transport=transport) == 0
    state.update(status="completed", partial=True)
    assert await collect_readonly(tmp_path, policy, ledger, transport=transport) == 0
    state.update(partial=False)
    other = policy.model_copy(update={"owner": "other"})
    assert await collect_readonly(tmp_path, other, ledger, transport=transport) == 0
    assert await collect_readonly(tmp_path, policy, ledger, transport=transport) == 1
    assert await collect_readonly(tmp_path, policy, ledger, transport=transport) == 0
    clean = LearningLedger(tmp_path, policy.model_copy(update={"state": "clean.sqlite"}))
    with sqlite3.connect(results.path) as db:
        db.execute("UPDATE tool_results SET payload='{}'")
    assert await collect_readonly(tmp_path, policy, clean, transport=transport) == 0


def test_memory_http_auth_identity_and_strict_inputs(memory, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core import settings
    from core.settings import AnalysisIdentityMode
    from service.memory_api import router

    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("auth-secret"))
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.SINGLE_USER)
    monkeypatch.setattr(settings, "ANALYSIS_SINGLE_USER_ID", "owner")
    app = FastAPI()
    app.state.memory_runtime = memory
    app.include_router(router)
    with TestClient(app) as http:
        assert http.get("/api/v1/hermes/memory").status_code == 401
        headers = {"Authorization": "Bearer auth-secret"}
        assert http.get("/api/v1/hermes/memory", headers=headers).json()["enabled"]
        response = http.post(
            "/api/v1/hermes/memory/corrections",
            headers=headers,
            json={"request_id": str(uuid4()), "content": "公里", "owner": "other"},
        )
        assert response.status_code == 422
        assert (
            http.put(
                "/api/v1/hermes/memory/settings", headers=headers, json={"enabled": "false"}
            ).status_code
            == 422
        )
        assert http.delete("/api/v1/hermes/memory", headers=headers).status_code == 202
        assert memory.status("other")["enabled"]
