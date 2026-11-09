import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from application.context import ExecutionContext
from application.knowledge import KnowledgeSearchService
from application.readonly import READ_ONLY_TOOLS, ReadOnlyApplication

CONTEXT = ExecutionContext("researcher", "thread-001")


@pytest.mark.asyncio
async def test_registered_portal_owners_can_read_shared_data_but_unknown_owner_cannot():
    calls = []

    async def operation(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    app = ReadOnlyApplication(
        {"search_scat_flights": operation},
        allowed_user_ids=frozenset({"scat-researcher", "scat-zyj", "scat-admin"}),
    )
    for owner in ["scat-researcher", "scat-zyj", "scat-admin"]:
        assert (await app.invoke("search_scat_flights", {}, context=ExecutionContext(owner, "qa")))[
            "status"
        ] == "ok"
    assert (
        await app.invoke("search_scat_flights", {}, context=ExecutionContext("unregistered", "qa"))
    )["status"] == "forbidden"
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("search_scat_flights", {"callsign": "sas123", "limit": 5}),
        ("get_scat_batch_overview", {}),
        ("get_scat_track_points", {"external_id": "SYNTH-001"}),
        ("inspect_scat_collection_data_quality", {}),
        ("search_scat_knowledge", {"query": "ADE"}),
    ],
)
async def test_operations_validate_and_invoke_shared_binding(name, args):
    received = []

    async def operation(**kwargs):
        received.append(kwargs)
        return {"status": "ok", "synthetic": True}

    app = ReadOnlyApplication({name: operation})
    assert (await app.invoke(name, args, context=CONTEXT))["status"] == "ok"
    assert len(received) == 1
    assert "user_id" not in received[0]
    if name == "get_scat_track_points":
        assert received[0]["limit"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("search_scat_flights", {"user_id": "other"}),
        ("search_scat_flights", {"mongo_query": {"$where": "unsafe"}}),
        ("get_scat_track_points", {"external_id": "SYNTH-001", "limit": 101}),
        ("inspect_scat_collection_data_quality", {"batch": None}),
        ("search_scat_knowledge", {"query": "   "}),
    ],
)
async def test_privileged_arguments_and_unbounded_inputs_refused(name, args):
    async def should_not_call(**_):
        raise AssertionError("backend called")

    app = ReadOnlyApplication({name: should_not_call})
    result = await app.invoke(name, args, context=CONTEXT)
    assert result["status"] == "invalid_request"
    assert "other" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unknown_tool_and_backend_failures_are_sanitized(caplog):
    async def fail(**_):
        raise RuntimeError("mongodb://private-secret@example")

    app = ReadOnlyApplication({"search_scat_flights": fail})
    assert (await app.invoke("terminal", {}, context=CONTEXT))["status"] == "tool_not_allowed"
    result = await app.invoke("search_scat_flights", {}, context=CONTEXT)
    assert result["status"] == "internal_error"
    assert "private-secret" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_output_budget_and_timeout(monkeypatch):
    async def large(**_):
        return {"status": "ok", "data": "x" * 200}

    app = ReadOnlyApplication({"search_scat_flights": large}, max_output_bytes=100)
    assert (await app.invoke("search_scat_flights", {}, context=CONTEXT))[
        "status"
    ] == "output_too_large"

    async def slow(**_):
        await asyncio.sleep(10)

    app = ReadOnlyApplication({"search_scat_flights": slow})
    monkeypatch.setitem(
        READ_ONLY_TOOLS,
        "search_scat_flights",
        replace(READ_ONLY_TOOLS["search_scat_flights"], timeout=0.01),
    )
    assert (await app.invoke("search_scat_flights", {}, context=CONTEXT))["status"] == "timeout"


@pytest.mark.asyncio
async def test_rag_does_not_require_mongo():
    async def connect():
        raise AssertionError("RAG must not depend on Mongo")

    async def retrieve(**_):
        return {"status": "ok", "data": {"source_id": "SYNTH-DOC"}}

    app = ReadOnlyApplication({"search_scat_knowledge": retrieve}, connect=connect)
    result = await app.invoke("search_scat_knowledge", {"query": "ADE"}, context=CONTEXT)
    assert result["data"]["source_id"] == "SYNTH-DOC"


@pytest.mark.asyncio
async def test_rag_failure_is_sanitized(caplog):
    class Factory:
        def __call__(self, **_):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *_, **__):
            raise httpx.ConnectError("private-secret-in-url")

    service = KnowledgeSearchService(
        enabled=True, base_url="http://rag", http_client_factory=Factory()
    )
    result = await service.search("ADE")
    assert result["status"] == "retrieval_error"
    assert "private-secret" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_real_stdio_handshake_discovery_calls_and_cleanup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(root / "scripts/run_scat_mcp.py"), "--synthetic"],
        cwd=str(root),
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "ANALYSIS_SINGLE_USER_ID": "test-researcher",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        info = await client.initialize()
        assert "SYNTHETIC" in info.serverInfo.name
        listing = await client.list_tools()
        assert {item.name for item in listing.tools} == set(READ_ONLY_TOOLS)
        assert all(item.inputSchema.get("additionalProperties") is False for item in listing.tools)
        good = await client.call_tool("get_scat_track_points", {"external_id": "SYNTH-001"})
        data = json.loads(good.content[0].text)
        assert data["synthetic"] is True and data["data"]["limit"] == 20
        bad = await client.call_tool("search_scat_flights", {"user_id": "other"})
        assert json.loads(bad.content[0].text)["status"] == "invalid_request"
        assert bad.isError is True
        assert good.isError is False


@pytest.mark.asyncio
async def test_oversized_track_page_is_explicit_summary_without_raw_objects():
    source = {
        "status": "ok",
        "data": {
            "pagination": {
                "returned_points": 100,
                "total_points": 3000,
                "next_start": 100,
                "has_more": True,
            },
            "points": [{"sequence": i, "quality": {"opaque": "x" * 1000}} for i in range(100)],
        },
    }

    async def query(**_):
        return source

    app = ReadOnlyApplication({"get_scat_track_points": query})
    result = await app.invoke(
        "get_scat_track_points", {"external_id": "SYNTH-001", "limit": 100}, context=CONTEXT
    )
    assert result["representation"] == "page_summary"
    assert result["data"]["summary"] == {"scope": "fetched_page", "points_omitted": 100}
    assert result["data"]["pagination"]["total_points"] == 3000
    assert "points" not in result["data"] and "opaque" not in json.dumps(result)
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 65536
    assert len(source["data"]["points"]) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", ["$where", "../private", " ", "bad/batch"])
async def test_illegal_batches_do_not_reach_business_service(batch):
    async def forbidden(**_):
        raise AssertionError("backend called")

    app = ReadOnlyApplication({"get_scat_batch_overview": forbidden})
    assert (await app.invoke("get_scat_batch_overview", {"batch": batch}, context=CONTEXT))[
        "status"
    ] == "invalid_request"


@pytest.mark.asyncio
async def test_fixed_identity_denies_foreign_context_before_backend_and_concurrent_apps_are_isolated():
    async def first(**_):
        await asyncio.sleep(0)
        return {"status": "ok", "data": "one-only"}

    async def second(**_):
        await asyncio.sleep(0)
        return {"status": "ok", "data": "two-only"}

    one = ReadOnlyApplication({"get_scat_batch_overview": first}, allowed_user_id="one")
    two = ReadOnlyApplication({"get_scat_batch_overview": second}, allowed_user_id="two")
    a, b = ExecutionContext("one", "thread"), ExecutionContext("two", "thread")
    results = await asyncio.gather(
        one.invoke("get_scat_batch_overview", {}, context=a),
        two.invoke("get_scat_batch_overview", {}, context=b),
        one.invoke("get_scat_batch_overview", {}, context=b),
    )
    assert [x.get("data") for x in results] == ["one-only", "two-only", None]
    assert results[-1]["status"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"limit": 0}, {"skip": -1}, {"limit": 101}])
async def test_invalid_flight_page_does_not_execute(arguments):
    async def forbidden(**_):
        raise AssertionError("backend called")

    app = ReadOnlyApplication({"search_scat_flights": forbidden})
    assert (await app.invoke("search_scat_flights", arguments, context=CONTEXT))[
        "status"
    ] == "invalid_request"


@pytest.mark.asyncio
async def test_missing_record_and_missing_trusted_identity_are_distinct():
    async def missing(**_):
        return {"status": "not_found", "data": None}

    app = ReadOnlyApplication({"get_scat_track_points": missing})
    assert (await app.invoke("get_scat_track_points", {"external_id": "missing"}, context=CONTEXT))[
        "status"
    ] == "not_found"
    with pytest.raises(TypeError):
        await app.invoke("get_scat_track_points", {"external_id": "missing"}, context=None)
