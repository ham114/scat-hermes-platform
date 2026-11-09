import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from application.context import ExecutionContext
from application.readonly import READ_ONLY_TOOLS, ReadOnlyApplication
from application.readonly_extensions import ALL_READ_ONLY_TOOLS, build_extended_business_application
from hermes_runtime.results import ToolResultStore, publish_tool_result


def arguments(name):
    fields = ALL_READ_ONLY_TOOLS[name].schema.model_fields
    values = {
        "external_id": "SYNTH-001",
        "timestamp": "2020-01-01T00:00:00Z",
        "query": "缺失率",
        "channels": ["cmp_comb_alt"],
    }
    return {k: values[k] for k, f in fields.items() if f.is_required()}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(ALL_READ_ONLY_TOOLS))
async def test_every_extended_operation_validates_and_returns_a_receipt(tmp_path, name):
    called = []

    async def operation(**kwargs):
        called.append(kwargs)
        return {"status": "ok", "synthetic": True, "data": {"value": 1}}

    app = ReadOnlyApplication({name: operation}, specs=ALL_READ_ONLY_TOOLS, allowed_user_id="one")
    context = ExecutionContext("one", "test")
    payload = await app.invoke(name, arguments(name), context=context)
    assert payload["status"] == "ok" and len(called) == 1
    store = ToolResultStore(tmp_path / "results.sqlite", workspace_root=tmp_path)
    ref = publish_tool_result(
        store,
        scope_key=context.scope_key,
        run_id="run_ext",
        call_id=name,
        tool="mcp__scat__" + name,
        result=payload,
    )
    assert store.get(context.scope_key, "run_ext", ref["result_id"])["operation"] == name
    refused = await app.invoke(name, {**arguments(name), "user_id": "other"}, context=context)
    assert refused["status"] == "invalid_request" and len(called) == 1
    assert (await app.invoke(name, arguments(name), context=ExecutionContext("other", "test")))[
        "status"
    ] == "forbidden"
    assert len(called) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("get_uav_samples", {"external_id": "SYNTH-001", "limit": 101}),
        ("get_uav_samples", {"external_id": "SYNTH-001", "channels": ["arbitrary"]}),
        (
            "get_uav_samples",
            {"external_id": "SYNTH-001", "fields": ["attitude"], "channels": ["cmp_comb_alt"]},
        ),
        ("search_uav_flights", {"takeoff_start": "2020-01-01T00:00:00"}),
        ("list_scat_quality_problem_flights", {"batch": None}),
        ("list_scat_quality_clean_flights", {"batch": "../private"}),
        ("list_scat_prediction_snapshots", {"external_id": "SYNTH-001", "limit": 101}),
        ("get_scat_prediction_snapshot", {"external_id": "SYNTH-001", "timestamp": "invalid"}),
    ],
)
async def test_bad_extended_parameters_never_reach_backend(name, args):
    backend = AsyncMock()
    app = ReadOnlyApplication({name: backend}, specs=ALL_READ_ONLY_TOOLS)
    assert (await app.invoke(name, args, context=ExecutionContext("one", "test")))[
        "status"
    ] == "invalid_request"
    backend.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_catalog_does_not_implicitly_expose_extensions():
    app = ReadOnlyApplication({})
    assert len(app.specs) == 5 and set(app.specs) == set(READ_ONLY_TOOLS)
    assert (await app.invoke("list_uav_models", {}, context=ExecutionContext("one", "test")))[
        "status"
    ] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_factory_maps_uav_time_fields_and_disables_persistent_quality_cache(monkeypatch):
    import scat.services.flight_service as scat_module
    import uav.services.uav_service as uav_module
    from core.settings import settings
    from scat.infrastructure.database import flight_database

    scat = Mock()
    uav = Mock()
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return scat

    monkeypatch.setattr(scat_module, "ScatFlightService", factory)
    monkeypatch.setattr(uav_module, "UavFlightService", lambda: uav)
    monkeypatch.setattr(flight_database, "connect", AsyncMock())
    uav.list_flights = AsyncMock(return_value={"status": "ok", "data": {}})
    uav.list_channels.return_value = {"status": "ok", "data": {"items": []}}
    app = build_extended_business_application()
    context = ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID, "test")
    result = await app.invoke(
        "search_uav_flights", {"takeoff_start": "2020-01-01T08:00:00+08:00"}, context=context
    )
    assert result["status"] == "ok"
    assert "takeoff_start" not in uav.list_flights.call_args.kwargs
    assert uav.list_flights.call_args.kwargs["start_time"].utcoffset().total_seconds() == 28800
    assert captured["quality_snapshot_cache"].enabled is False
    flight_database.connect.reset_mock()
    assert (await app.invoke("list_uav_channels", {}, context=context))["status"] == "ok"
    flight_database.connect.assert_not_called()


@pytest.mark.asyncio
async def test_real_extended_stdio_discovery_and_bounded_samples(tmp_path):
    root = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(root / "scripts/run_scat_mcp.py"), "--synthetic", "--skill-tools"],
        cwd=str(root),
        env={
            "TMPDIR": str(tmp_path),
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        listing = await client.list_tools()
        assert {x.name for x in listing.tools} == set(ALL_READ_ONLY_TOOLS)
        assert all(x.annotations.readOnlyHint for x in listing.tools)
        query = await client.call_tool(
            "search_scat_flights", {"batch": "SYNTH", "callsign": "TEST123", "limit": 1}
        )
        payload = json.loads(query.content[0].text)
        assert (
            payload["synthetic"]
            and payload["data"]["items"][0]["source"]["external_id"] == "SYNTH-001"
        )
        invalid = await client.call_tool(
            "get_uav_samples", {"external_id": "SYNTH-001", "limit": 101}
        )
        assert (
            invalid.isError and json.loads(invalid.content[0].text)["status"] == "invalid_request"
        )
