"""Preference scopes, memory lifecycle and real publication race boundaries."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from application.context import ExecutionContext
from hermes_runtime.autonomy_config import MemoryPolicy
from hermes_runtime.honcho_memory import MemoryRuntime
from service.auth import verify_bearer
from service.hermes_api import get_hermes_owner
from service.memory_api import router
from tests.visualization_runtime.test_delivery import parent
from tests.visualization_runtime.test_pipeline import pipeline as _pipeline
from visualization_runtime.contracts import CreateVisualization
from visualization_runtime.preferences import PreferenceEdit, extract
from visualization_runtime.snapshots import digest
from visualization_runtime.store import JobConflict

pipeline = _pipeline


class FakeHoncho:
    def __init__(self):
        self.reads = 0
        self.writes = []
        self.deleted = []

    async def recall(self, workspace, query=""):
        self.reads += 1
        return '{"representation":"旧远端摘要"}'

    async def write(self, workspace, session, event_id, content):
        self.writes.append((workspace, content))

    async def delete(self, workspace):
        self.deleted.append(workspace)
        return True


@pytest.fixture
def memory(tmp_path):
    (tmp_path / "identity.key").write_text("test-identity-" + "x" * 32)
    return MemoryRuntime(
        tmp_path,
        MemoryPolicy(state="memory.sqlite", identity_key="identity.key"),
        client=FakeHoncho(),
    )


def edit(memory, owner, key, value, *, task_type="all", request_id=None):
    state = memory.charts.list(owner)
    body = PreferenceEdit(
        request_id=request_id or uuid4().hex,
        epoch=state["epoch"],
        revision=state["revision"],
        key=key,
        value=value,
        task_type=task_type,
    )
    return memory.charts.edit(owner, body)


def admitted(memory, context, text):
    request = uuid4()
    asyncio.run(memory.prepare(context, text, request))
    memory.admitted(context, request)
    return request


def test_only_explicit_future_preferences_are_durable(memory):
    context = ExecutionContext("alice", "one")
    admitted(memory, context, "这张图换成蓝色系，字号18")
    admitted(memory, context, "我更喜欢蓝色系")
    assert memory.charts.list("alice")["items"] == []
    assert memory.status("alice")["queue"] == {}
    request = uuid4()
    asyncio.run(memory.prepare(context, "以后所有图表都用蓝色系，字号18", request))
    assert memory.charts.list("alice")["items"] == []
    memory.admitted(context, request)
    state = memory.charts.list("alice")
    assert {r["key"]: r["value"] for r in state["items"]} == {"palette": "blue", "font_size": 18}
    memory.admitted(context, request)
    assert memory.charts.list("alice")["revision"] == state["revision"]
    assert memory.charts.list("bob")["items"] == []
    assert memory.client.writes == []  # Local changes do not wait on remote writes.


@pytest.mark.parametrize(
    "text",
    [
        "不要记住这张图用蓝色",
        "以后把密码 sk-abcdefghijklmnopqrst 放在图上",
        "这个航班以后应该安全",
    ],
)
def test_secrets_negations_and_business_conclusions_are_not_style_memory(text):
    assert extract(text, durable=True) == {}


def test_explicit_negative_palette_is_saved_as_an_avoidance_hint():
    assert extract("以后不要用红色", durable=True) == {
        "palette": "avoid_red",
        "special_needs": "避免使用红色",
    }
    assert extract("请记住：用公里", durable=True) == {"units": "km"}


@pytest.mark.parametrize(
    "text", ["以后所有图表蓝色系，但这张图要红色", "这张图要红色，但以后所有图表都用蓝色系"]
)
def test_mixed_current_and_future_scopes_are_separate(text):
    assert extract(text, durable=True) == {"palette": "blue"}
    assert extract(text) == {"palette": "red"}


def test_quoted_examples_are_not_explicit_preference_instructions():
    assert extract("请解释“以后图表都用红色”这句话", durable=True) == {}
    assert extract("以后图表都用“蓝色系”", durable=True) == {"palette": "blue"}


def test_correction_scopes_and_current_chart_override_global(pipeline, memory):
    app, context, job, *_ = pipeline
    app.preferences = memory.charts
    parent(app, context)
    edit(memory, context.user_id, "palette", "blue")
    edit(memory, context.user_id, "font_size", 16)
    edit(memory, context.user_id, "palette", "red", task_type="trajectory")
    app.jobs.cancel(context, job["task_id"])
    refs = app.require_snapshot(context, job["task_id"])["payload"]["datasets"][0]["reference"]
    first = asyncio.run(
        app.create(
            context,
            CreateVisualization(request_id="scope", intent="绘制轨迹", data_refs=[refs]),
            parent_run_id="run_fixture",
        )
    )
    assert first["request"].preferences == {"palette": "red", "font_size": 16}
    second = asyncio.run(
        app.create(
            context,
            CreateVisualization(
                request_id="override", intent="这张图换绿色，字号20", data_refs=[refs]
            ),
            parent_run_id="run_fixture",
        )
    )
    assert second["request"].preferences == {"palette": "green", "font_size": 20}
    assert {
        r["value"] for r in memory.charts.list(context.user_id)["items"] if r["key"] == "palette"
    } == {"blue", "red"}


def test_edits_are_idempotent_and_concurrent_old_revisions_cannot_overwrite(memory):
    state = memory.charts.list("alice")
    body = PreferenceEdit(
        request_id="stable",
        epoch=state["epoch"],
        revision=state["revision"],
        key="palette",
        value="blue",
    )
    first = memory.charts.edit("alice", body)
    assert memory.charts.edit("alice", body) == first

    def change(value):
        try:
            return memory.charts.edit(
                "alice",
                PreferenceEdit(
                    request_id=value,
                    epoch=first["epoch"],
                    revision=first["revision"],
                    key="palette",
                    value=value,
                ),
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(change, ["red", "green"]))
    assert sum(r is not None for r in rows) == 1


def test_pause_delete_and_resume_never_replay_old_user_statements(memory):
    context = ExecutionContext("alice", "one")
    admitted(memory, context, "以后图表都用蓝色系")
    stale = memory.charts.list("alice")
    old = uuid4()
    asyncio.run(memory.prepare(context, "以后图表都用红色系", old))
    memory.settings("alice", False)
    memory.settings("alice", True)
    memory.admitted(context, old)
    assert memory.charts.list("alice")["items"][0]["value"] == "blue"
    memory.forget("alice")
    memory.settings("alice", True)
    memory.admitted(context, old)
    with pytest.raises(ValueError):
        memory.charts.edit(
            "alice",
            PreferenceEdit(
                request_id="stale",
                epoch=stale["epoch"],
                revision=stale["revision"],
                key="palette",
                value="red",
            ),
        )
    assert memory.charts.list("alice")["items"] == []
    with memory.connect() as db:
        assert db.execute("SELECT count(*) FROM chart_preferences").fetchone()[0] == 0
        assert not any(
            r[0] for r in db.execute("SELECT content FROM memory_outbox WHERE kind!='delete'")
        )
        assert not any(r[0] for r in db.execute("SELECT instructions FROM memory_requests"))
    asyncio.run(memory.drain())
    assert memory.client.writes == [] and memory.client.deleted


def test_single_key_delete_scrubs_basis_and_respects_pause(memory):
    context = ExecutionContext("alice", "one")
    admitted(memory, context, "以后图表用蓝色系，字号18")
    memory.settings("alice", False)
    edit(memory, "alice", "palette", None)
    state = memory.charts.list("alice")
    assert [r["key"] for r in state["items"]] == ["font_size"]
    assert all(not r["evidence"] for r in state["items"])
    asyncio.run(memory.drain())
    assert memory.client.writes == []
    memory.settings("alice", True)
    assert [r["key"] for r in memory.charts.list("alice")["items"]] == ["font_size"]


def prepare_job(pipeline, memory):
    app, context, old, *_ = pipeline
    app.preferences = memory.charts
    parent(app, context)
    edit(memory, context.user_id, "palette", "blue")
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    app.jobs.cancel(context, old["task_id"])
    job = asyncio.run(
        app.create(
            context,
            CreateVisualization(request_id="drawing", intent="绘制本页轨迹", data_refs=[refs]),
            parent_run_id="run_fixture",
        )
    )
    return app, context, job


def test_forget_during_generation_regenerates_without_old_preferences(pipeline, memory):
    app, context, job = prepare_job(pipeline, memory)
    worker, calls = pipeline[3:5]
    seen = []
    transport = worker.code.transport

    def code(request):
        seen.append(
            json.loads(json.loads(request.content)["messages"][1]["content"])["preferences"]
        )
        if len(seen) == 1:
            memory.forget(context.user_id)
        return transport.handle_request(request)

    worker.code.transport = httpx.MockTransport(code)
    asyncio.run(worker.run_once())
    result = app.jobs.get(context, job["task_id"])
    assert result["state"] == "succeeded"
    assert seen == [{"palette": "blue"}, {}]
    assert result["request"].preferences == {} and calls == {"code": 2, "review": 1}
    assert memory.charts.list(context.user_id)["items"] == []


def test_forget_between_image_copy_and_commit_rejects_old_pass(pipeline, memory, monkeypatch):
    app, context, job = prepare_job(pipeline, memory)
    worker, calls = pipeline[3:5]
    original = worker.launcher.image_bytes
    forgotten = False

    def image(*args):
        nonlocal forgotten
        result = original(*args)
        root = app.snapshots.root / "published"
        if not forgotten and root.is_dir() and any(root.iterdir()):
            forgotten = True
            memory.forget(context.user_id)
        return result

    monkeypatch.setattr(worker.launcher, "image_bytes", image)
    asyncio.run(worker.run_once())
    result = app.view(context, job["task_id"])
    assert forgotten and result.state == "succeeded"
    assert calls == {"code": 2, "review": 2}
    assert app.publisher.get(context, result.artifact_id)["preferences_sha256"] == digest({})
    with app.jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_publications").fetchone()[0] == 1


def test_model_arguments_never_teach_long_term_preferences_and_stale_user_token_fails(
    pipeline, memory
):
    app, context, old, *_ = pipeline
    app.preferences = memory.charts
    parent(app, context)
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    state = memory.charts.list(context.user_id)
    body = CreateVisualization(
        request_id="model",
        intent="以后图表都用蓝色",
        data_refs=[refs],
        memory_epoch=state["epoch"],
        memory_revision=state["revision"],
    )
    asyncio.run(app.create(context, body, parent_run_id="run_fixture"))
    assert memory.charts.list(context.user_id)["items"] == []
    memory.forget(context.user_id)
    memory.settings(context.user_id, True)
    with pytest.raises(JobConflict, match="memory_changed"):
        asyncio.run(
            app.create(
                context,
                body.model_copy(update={"request_id": "late-user"}),
                parent_run_id="run_fixture",
                learn=True,
            )
        )
    assert memory.charts.list(context.user_id)["items"] == []


def test_memory_api_is_owner_scoped_and_requires_epoch_revision(memory):
    web = FastAPI()
    web.include_router(router)
    web.state.memory_runtime = memory
    web.dependency_overrides[verify_bearer] = lambda: None
    web.dependency_overrides[get_hermes_owner] = lambda: "alice"

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://test"
        ) as client:
            path = "/api/v1/hermes/memory/chart-preferences"
            state = (await client.get(path)).json()
            body = {
                "request_id": "api",
                "epoch": state["epoch"],
                "revision": state["revision"],
                "key": "palette",
                "value": "blue",
            }
            assert (await client.post(path, json=body)).status_code == 200
            assert (await client.post(path, json=body | {"owner": "bob"})).status_code == 422
            web.dependency_overrides[get_hermes_owner] = lambda: "bob"
            assert (await client.get(path)).json()["items"] == []
            web.dependency_overrides[get_hermes_owner] = lambda: "alice"
            assert (await client.delete("/api/v1/hermes/memory")).status_code == 202
            assert (await client.post(path, json=body)).status_code == 409

    asyncio.run(exercise())


def test_explicit_user_api_preference_is_immediate_cross_thread_and_replay_safe(pipeline, memory):
    app, context, old, *_ = pipeline
    app.preferences = memory.charts
    parent(app, context)
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    state = memory.charts.list(context.user_id)
    body = CreateVisualization(
        request_id="user-blue",
        intent="以后所有图表都用蓝色系",
        data_refs=[refs],
        memory_epoch=state["epoch"],
        memory_revision=state["revision"],
    )
    first = asyncio.run(app.create(context, body, parent_run_id="run_fixture", learn=True))
    assert memory.charts.list(context.user_id)["items"][0]["value"] == "blue"
    assert (
        asyncio.run(app.create(context, body, parent_run_id="run_fixture", learn=True))["task_id"]
        == first["task_id"]
    )
    other = ExecutionContext(context.user_id, "another-thread")
    parent(app, other)
    receipt = app.receipts.put(
        other.scope_key,
        "run_fixture",
        "new-call",
        "get_scat_track_points",
        {"status": "ok", "data": {"points": [{"x": 1, "y": 2}]}},
    )
    reference = {
        "kind": "readonly_result",
        "source_id": receipt["result_id"],
        "run_id": "run_fixture",
        "payload_sha256": receipt["payload_sha256"],
    }
    second = asyncio.run(
        app.create(
            other,
            CreateVisualization(
                request_id="other-thread", intent="绘制轨迹", data_refs=[reference]
            ),
            parent_run_id="run_fixture",
        )
    )
    assert second["request"].preferences == {"palette": "blue"}
    memory.forget(context.user_id)
    assert (
        asyncio.run(app.create(context, body, parent_run_id="run_fixture", learn=True))["task_id"]
        == first["task_id"]
    )
    assert memory.charts.list(context.user_id)["items"] == []


def test_pause_resume_during_review_invalidates_same_style_old_pass(pipeline, memory):
    app, context, job = prepare_job(pipeline, memory)
    worker, calls = pipeline[3:5]
    transport = worker.reviewer.provider.transport
    changed = False

    def review(request):
        nonlocal changed
        if not changed:
            changed = True
            memory.settings(context.user_id, False)
            memory.settings(context.user_id, True)
        return transport.handle_request(request)

    worker.reviewer.provider.transport = httpx.MockTransport(review)
    asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).state == "succeeded"
    assert calls == {"code": 2, "review": 2}
    assert app.jobs.get(context, job["task_id"])["request"].preferences_round == 1


def test_preference_refresh_does_not_reset_candidate_or_deadline_budget(pipeline, memory):
    app, context, job = prepare_job(pipeline, memory)
    worker, calls = pipeline[3:5]
    worker.settings = worker.settings.model_copy(update={"max_revisions": 0})
    with app.jobs.transaction() as db:
        deadline = db.execute(
            "SELECT deadline FROM visualization_job_budgets WHERE task_id=?", (job["task_id"],)
        ).fetchone()[0]
    transport = worker.code.transport

    def code(request):
        memory.forget(context.user_id)
        return transport.handle_request(request)

    worker.code.transport = httpx.MockTransport(code)
    asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).state == "failed" and calls == {"code": 1, "review": 0}
    with app.jobs.transaction() as db:
        assert (
            db.execute(
                "SELECT deadline FROM visualization_job_budgets WHERE task_id=?", (job["task_id"],)
            ).fetchone()[0]
            == deadline
        )
        assert db.execute("SELECT count(*) FROM visualization_publications").fetchone()[0] == 0


def test_generator_and_reviewer_receive_the_same_frozen_preferences(pipeline, memory):
    app, context, job = prepare_job(pipeline, memory)
    worker = pipeline[3]
    code_transport = worker.code.transport
    review_transport = worker.reviewer.provider.transport
    observed = {}

    def code(request):
        observed["code"] = json.loads(json.loads(request.content)["messages"][1]["content"])[
            "preferences"
        ]
        return code_transport.handle_request(request)

    def review(request):
        observed["review"] = json.loads(
            json.loads(request.content)["messages"][1]["content"][0]["text"]
        )["preferences"]
        return review_transport.handle_request(request)

    worker.code.transport = httpx.MockTransport(code)
    worker.reviewer.provider.transport = httpx.MockTransport(review)
    asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).state == "succeeded"
    assert observed == {"code": {"palette": "blue"}, "review": {"palette": "blue"}}


def test_failed_job_commit_rolls_back_preference_and_parent_budget(pipeline, memory):
    import sqlite3

    app, context, old, *_ = pipeline
    app.preferences = memory.charts
    budget = parent(app, context)
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    state = memory.charts.list(context.user_id)
    with app.jobs.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_job BEFORE INSERT ON visualization_jobs BEGIN SELECT RAISE(ABORT,'fixture_failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture_failed"):
        asyncio.run(
            app.create(
                context,
                CreateVisualization(
                    request_id="failed",
                    intent="以后图表蓝色系",
                    data_refs=[refs],
                    memory_epoch=state["epoch"],
                    memory_revision=state["revision"],
                ),
                parent_run_id="run_fixture",
                learn=True,
            )
        )
    assert memory.charts.list(context.user_id)["items"] == []
    with sqlite3.connect(budget) as db:
        assert db.execute("SELECT charged_tokens FROM skill_run_budgets").fetchone()[0] == 0


def test_change_between_reservation_and_send_aborts_unspent_call(pipeline, memory, monkeypatch):
    app, context, job = prepare_job(pipeline, memory)
    worker, calls = pipeline[3:5]
    original = worker.ledger.start_call
    changed = False

    def start(*args):
        nonlocal changed
        if not changed:
            changed = True
            memory.forget(context.user_id)
        return original(*args)

    monkeypatch.setattr(worker.ledger, "start_call", start)
    asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).state == "succeeded"
    assert calls == {"code": 1, "review": 1}
    with app.jobs.transaction() as db:
        assert (
            db.execute(
                "SELECT charged_tokens FROM visualization_provider_calls WHERE state='aborted'"
            ).fetchone()[0]
            == 0
        )


def test_native_question_is_exact_owner_scoped_and_integrity_checked(tmp_path):
    import hashlib

    from hermes_runtime.registry import RunRegistry

    registry = RunRegistry(tmp_path / "registry.sqlite", workspace_root=tmp_path)
    thread = registry.create_thread("alice")["thread_id"]
    context = ExecutionContext("alice", thread)
    question = "请绘制本页数据，蓝色只用于这张图，不要记住。"
    request = uuid4().hex
    registry.reserve_request(
        context, request, hashlib.sha256(question.encode()).hexdigest(), question
    )
    registry.bind(context, request, "run_native")
    assert registry.original_question(context, "run_native") == question
    with pytest.raises(LookupError):
        registry.original_question(ExecutionContext("bob", thread), "run_native")
    with registry._connect() as db:
        db.execute("UPDATE conversation_turns SET input='篡改的用户偏好'")
    with pytest.raises(ValueError, match="trusted_user_message"):
        registry.original_question(context, "run_native")


def test_legacy_memory_table_writes_and_lifecycle_remain_compatible(memory):
    context = ExecutionContext("alice", "legacy-thread")
    request = uuid4()
    identity = context.scope_key + ":" + str(request)
    owner = memory.owner(context.user_id)
    with memory.connect() as db:
        assert len(list(db.execute("PRAGMA table_info(memory_users)"))) == 4
        assert len(list(db.execute("PRAGMA table_info(memory_requests)"))) == 9
        db.execute("INSERT INTO memory_users VALUES(?,1,1,0)", (owner,))
        db.execute(
            "INSERT INTO memory_requests VALUES(?,?,1,'sha','','以后图表红色系','legacy','prepared',0)",
            (identity, owner),
        )
    edit(memory, context.user_id, "palette", "blue")
    with memory.connect() as db:
        db.execute("UPDATE memory_users SET enabled=0 WHERE owner=?", (owner,))
        db.execute("UPDATE memory_users SET enabled=1 WHERE owner=?", (owner,))
    memory.admitted(context, request)
    assert memory.charts.list(context.user_id)["items"][0]["value"] == "blue"
    with memory.connect() as db:
        db.execute("UPDATE memory_users SET epoch=epoch+1,enabled=0 WHERE owner=?", (owner,))
    assert memory.charts.list(context.user_id)["items"] == []


def test_publication_gate_rejects_old_pass_after_same_style_rebinding(
    pipeline, memory, monkeypatch
):
    from visualization_runtime.preferences import refresh

    app, context, job = prepare_job(pipeline, memory)
    worker = pipeline[3]
    original = worker.publisher.publish
    checked = False

    def publish(ctx, task, worker_id, generation, render_id):
        nonlocal checked
        if not checked:
            checked = True
            memory.settings(context.user_id, False)
            memory.settings(context.user_id, True)
            assert refresh(app, ctx, task, worker_id, generation)
            # Fault injection: a buggy caller attempts the old approved render
            # after rebinding the task. The publisher itself must reject it.
            with app.jobs.transaction() as db:
                db.execute(
                    "UPDATE visualization_jobs SET phase='publishing' WHERE task_id=?", (task,)
                )
            with pytest.raises(JobConflict, match="publication_evidence_changed"):
                original(ctx, task, worker_id, generation, render_id)
            with app.jobs.transaction() as db:
                db.execute(
                    "UPDATE visualization_jobs SET phase='generating' WHERE task_id=?", (task,)
                )
            raise JobConflict("visualization_preferences_changed")
        return original(ctx, task, worker_id, generation, render_id)

    monkeypatch.setattr(worker.publisher, "publish", publish)
    asyncio.run(worker.run_once())
    assert checked and app.view(context, job["task_id"]).state == "succeeded"


def test_unsupported_shared_journal_fails_before_preference_or_budget_write(pipeline, memory):
    import sqlite3

    app, context, job = prepare_job(pipeline, memory)
    refs = app.require_snapshot(context, job["task_id"])["payload"]["datasets"][0]["reference"]
    state = memory.charts.list(context.user_id)
    with memory.connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    with pytest.raises(JobConflict, match="shared_journal_unsupported"):
        asyncio.run(
            app.create(
                context,
                CreateVisualization(
                    request_id="wal",
                    intent="以后图表红色系",
                    data_refs=[refs],
                    memory_epoch=state["epoch"],
                    memory_revision=state["revision"],
                ),
                parent_run_id="run_fixture",
                learn=True,
            )
        )
    assert memory.charts.list(context.user_id)["items"][0]["value"] == "blue"
    with sqlite3.connect(app.parent_budget_path) as db:
        assert db.execute("SELECT charged_tokens FROM skill_run_budgets").fetchone()[0] == 60000
    with memory.connect() as db:
        db.execute("PRAGMA journal_mode=DELETE")
