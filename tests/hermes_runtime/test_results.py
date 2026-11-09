from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from application.context import ExecutionContext
from hermes_runtime.results import (
    ResultNotFoundError,
    ToolResultStore,
    publish_tool_result,
    tool_payload,
)

SCOPE = ExecutionContext("one", "thread-one").scope_key
OTHER = ExecutionContext("two", "thread-two").scope_key
RUN = "run_" + uuid4().hex
PAYLOAD = {"status": "ok", "data": {"total": 10637}}


def store(tmp_path, **kw):
    return ToolResultStore(tmp_path / "results.sqlite", workspace_root=tmp_path, **kw)


def test_scoped_receipts_survive_reopen_and_replay(tmp_path):
    first = store(tmp_path)
    ref = first.put(SCOPE, RUN, "call-1", "search_scat_flights", PAYLOAD)
    assert first.put(SCOPE, RUN, "call-1", "search_scat_flights", PAYLOAD) == ref
    reopened = store(tmp_path)
    assert reopened.get(SCOPE, RUN, ref["result_id"])["result"] == PAYLOAD
    assert reopened.list(SCOPE, RUN) == [ref]
    assert reopened.list(OTHER, RUN) == []
    for scope, run in [(OTHER, RUN), (SCOPE, "run_other")]:
        with pytest.raises(ResultNotFoundError):
            reopened.get(scope, run, ref["result_id"])
    with pytest.raises(ValueError, match="changed"):
        first.put(SCOPE, RUN, "call-1", "search_scat_flights", {"status": "ok", "data": 42})


def test_receipt_expiry_capacity_and_integrity(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_runtime.results.time.time", lambda: 100)
    db = store(tmp_path, ttl=10, max_rows=1)
    ref = db.put(SCOPE, RUN, "c1", "search_scat_flights", PAYLOAD)
    with pytest.raises(ValueError, match="full"):
        db.put(SCOPE, RUN, "c2", "search_scat_flights", PAYLOAD)
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE tool_results SET payload=?", ("{}",))
    with pytest.raises(ValueError, match="integrity"):
        db.get(SCOPE, RUN, ref["result_id"])
    monkeypatch.setattr("hermes_runtime.results.time.time", lambda: 111)
    with pytest.raises(ResultNotFoundError):
        db.get(SCOPE, RUN, ref["result_id"])
    assert db.list(SCOPE, RUN) == []
    assert db.put(SCOPE, RUN, "c2", "search_scat_flights", PAYLOAD)


def test_concurrent_publishers_have_exactly_one_stable_receipt(tmp_path):
    db = store(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as workers:
        refs = list(
            workers.map(
                lambda _: db.put(SCOPE, RUN, "same", "search_scat_flights", PAYLOAD), range(8)
            )
        )
    assert len({r["result_id"] for r in refs}) == 1
    assert len(db.list(SCOPE, RUN)) == 1


@pytest.mark.asyncio
async def test_concurrent_scopes_do_not_share_payloads(tmp_path):
    db = store(tmp_path)
    refs = await asyncio.gather(
        *[
            asyncio.to_thread(
                db.put, scope, RUN, "same", "search_scat_flights", {"status": "ok", "data": label}
            )
            for scope, label in [(SCOPE, "one-only"), (OTHER, "two-only")]
        ]
    )
    assert db.get(SCOPE, RUN, refs[0]["result_id"])["result"]["data"] == "one-only"
    with pytest.raises(ResultNotFoundError):
        db.get(SCOPE, RUN, refs[1]["result_id"])


def test_only_real_allowlisted_mcp_envelopes_publish(tmp_path):
    db = store(tmp_path)
    wrapped = {"result": json.dumps([{"type": "text", "text": json.dumps(PAYLOAD)}])}
    ref = publish_tool_result(
        db,
        scope_key=SCOPE,
        run_id=RUN,
        call_id="c1",
        tool="mcp__scat__search_scat_flights",
        result=wrapped,
    )
    assert db.get(SCOPE, RUN, ref["result_id"])["result"] == PAYLOAD
    for tool, payload in [
        ("terminal", wrapped),
        ("mcp__scat__terminal", wrapped),
        ("mcp__scat__search_scat_flights", {"status": "internal_error", "data": None}),
    ]:
        assert (
            publish_tool_result(
                db, scope_key=SCOPE, run_id=RUN, call_id="x", tool=tool, result=payload
            )
            is None
        )
    assert tool_payload("x" * 262145) is None
    with pytest.raises(ValueError):
        db.put("client-supplied-user", RUN, "c2", "search_scat_flights", PAYLOAD)
    with pytest.raises(ValueError):
        store(tmp_path, max_bytes=1).put(SCOPE, RUN, "c2", "search_scat_flights", PAYLOAD)
    with pytest.raises(ValueError):
        ToolResultStore(tmp_path.parent / "outside.sqlite", workspace_root=tmp_path)
