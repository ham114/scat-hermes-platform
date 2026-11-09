import hashlib
import sqlite3
from uuid import uuid4

import pytest

from application.context import ExecutionContext
from hermes_runtime.registry import (
    RequestConflictError,
    RunRegistry,
    ThreadBusyError,
    ThreadNotFoundError,
)
from tests.hermes_runtime.test_platform_api import api  # noqa: F401

BASE = "/api/v1/hermes"


def test_oversize_terminal_reply_is_not_presented_as_complete_partial(tmp_path):
    reg = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    ctx = reg.require_thread("owner", reg.create_thread("owner")["thread_id"])
    _, run = question(reg, ctx, "问题")
    reg.save_delta(ctx, run, 1, "开头片段")
    reg.save_turn(ctx, {"run_id": run, "status": "completed", "output": "x" * (2 * 1024**2 + 1)})
    row = reg.history_turns(ctx)["items"][0]
    assert row["status"] == "completed" and row["output"] is None


def test_thread_pages_use_owner_bound_keysets(tmp_path):
    reg = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    for _ in range(4):
        reg.create_thread("owner")
    first = reg.history_threads("owner", 2)
    reg.create_thread("owner")
    second = reg.history_threads("owner", 2, first["next_cursor"])
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert not {x["thread_id"] for x in first["items"]} & {x["thread_id"] for x in second["items"]}
    with pytest.raises(ValueError):
        reg.history_threads("other", 2, first["next_cursor"])


def question(reg, context, text):
    identity = str(uuid4())
    reg.reserve_request(context, identity, hashlib.sha256(text.encode()).hexdigest(), text)
    run = "run_" + uuid4().hex
    reg.bind(context, identity, run)
    return identity, run


def test_journal_replay_restart_terminal_and_partial(tmp_path):
    reg = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    tid = reg.create_thread("owner")["thread_id"]
    ctx = reg.require_thread("owner", tid)
    identity, run = question(reg, ctx, "我的原始问题")
    reg.reserve_request(
        ctx, identity, hashlib.sha256("我的原始问题".encode()).hexdigest(), "我的原始问题"
    )
    with pytest.raises(RequestConflictError):
        reg.reserve_request(ctx, identity, hashlib.sha256(b"changed").hexdigest(), "changed")
    reg.save_delta(ctx, run, 1, "部分")
    reg.save_turn(ctx, {"run_id": run, "status": "running", "output": "部分回复"})
    reg.save_delta(ctx, run, 1, "重复")
    reg.save_delta(ctx, run, 2, "回复")
    assert reg.history_turns(ctx)["items"][0]["output"] == "部分回复"
    reg.save_turn(
        ctx, {"run_id": run, "status": "completed", "output": "完整回复", "reasoning": "PRIVATE"}
    )
    reg.save_turn(ctx, {"run_id": run, "status": "running", "output": "迟到"})
    reg = RunRegistry(reg.path, workspace_root=tmp_path)
    page = reg.history_turns(ctx)
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert (row["input"], row["output"], row["status"]) == ("我的原始问题", "完整回复", "completed")
    assert row["last_sequence"] == 2 and "reasoning" not in row


def test_busy_admission_rolls_back_history_and_title(tmp_path):
    reg = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    tid = reg.create_thread("owner")["thread_id"]
    ctx = reg.require_thread("owner", tid)
    question(reg, ctx, "第一个问题")
    with pytest.raises(ThreadBusyError):
        question(reg, ctx, "第二个问题")
    assert len(reg.history_turns(ctx)["items"]) == 1
    assert reg.history_threads("owner")["items"][0]["title"] == "第一个问题"


def test_keyset_pages_are_scoped_and_new_turn_does_not_duplicate(tmp_path):
    reg = RunRegistry(tmp_path / "state.sqlite", workspace_root=tmp_path)
    tid = reg.create_thread("owner")["thread_id"]
    ctx = reg.require_thread("owner", tid)
    for i in range(5):
        _, run = question(reg, ctx, str(i))
        reg.finish_run(ctx, run)
    page = reg.history_turns(ctx, 2)
    assert [r["input"] for r in page["items"]] == ["3", "4"]
    question(reg, ctx, "new")
    page2 = reg.history_turns(ctx, 2, page["next_cursor"])
    assert [r["input"] for r in page2["items"]] == ["1", "2"]
    other = ExecutionContext("other", tid)
    with pytest.raises(ValueError):
        reg.history_turns(other, 2, page["next_cursor"])
    assert reg.history_turns(other)["items"] == []
    with pytest.raises(ValueError):
        reg.history_turns(ctx, 2, "garbage")
    with pytest.raises(ThreadNotFoundError):
        reg.rename_thread("other", tid, "越权", 1)


def test_old_bindings_remain_honestly_missing_and_rename_is_optimistic(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE runtime_runs(run_id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,request_id TEXT NOT NULL,UNIQUE(scope_key,request_id))"
        )
        db.execute(
            "CREATE TABLE runtime_threads(thread_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,created_at REAL NOT NULL)"
        )
        ctx = ExecutionContext("owner", str(uuid4()))
        db.execute("INSERT INTO runtime_threads VALUES(?,?,?)", (ctx.thread_id, "owner", 1))
        db.execute(
            "INSERT INTO runtime_runs VALUES(?,?,?)", ("run_old", ctx.scope_key, str(uuid4()))
        )
    reg = RunRegistry(path, workspace_root=tmp_path)
    row = reg.history_turns(ctx)["items"][0]
    assert row["input"] is None and not row["input_available"] and row["created_at"] is None
    assert reg.rename_thread("owner", ctx.thread_id, "自定义标题", 0)["title_revision"] == 1
    with pytest.raises(ValueError, match="conflict"):
        reg.rename_thread("owner", ctx.thread_id, "覆盖", 0)
    with pytest.raises(ValueError):
        reg.rename_thread("owner", ctx.thread_id, "换行\n标题", 1)


@pytest.mark.asyncio
async def test_http_complete_history_titles_and_owner_cursor(api):  # noqa: F811
    client, native, _registry, _app = api
    tid = (await client.post(BASE + "/threads", json={})).json()["thread_id"]
    body = {"request_id": str(uuid4()), "message": "完整提问：单位是什么？"}
    admission = await client.post(BASE + f"/threads/{tid}/runs", json=body)
    assert admission.status_code == 202
    run = admission.json()["run_id"]
    # SSE captures the actual final reply, not a model's fabricated history.
    assert (await client.get(BASE + f"/threads/{tid}/runs/{run}/events")).status_code == 200
    page = (await client.get(BASE + f"/threads/{tid}/messages")).json()
    assert page["items"][0]["input"] == body["message"]
    assert page["items"][0]["output"] == "OK"
    count = len([r for r in native.requests if r.method == "POST"])
    assert (await client.get(BASE + f"/threads/{tid}/messages")).status_code == 200
    assert len([r for r in native.requests if r.method == "POST"]) == count
    thread = (await client.get(BASE + "/threads")).json()["items"][0]
    assert thread["title"] == body["message"]
    changed = await client.patch(
        BASE + f"/threads/{tid}",
        json={"title": "新标题", "expected_revision": thread["title_revision"]},
    )
    assert changed.status_code == 200
    assert (
        await client.patch(
            BASE + f"/threads/{tid}",
            json={"title": "旧请求", "expected_revision": thread["title_revision"]},
        )
    ).status_code == 409
    assert (await client.get(BASE + f"/threads/{uuid4()}/messages")).status_code == 404
    assert (await client.get(BASE + f"/threads/{tid}/messages?cursor=invalid")).status_code == 422
    assert (await client.get(BASE + f"/threads/{tid}/messages?limit=101")).status_code == 422
