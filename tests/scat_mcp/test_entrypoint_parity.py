"""Same synthetic repository through REST, legacy Tool, and actual MCP protocol."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import anyio
import httpx
import pytest
from fastapi import FastAPI
from mcp import ClientSession

from application.context import ExecutionContext
from application.readonly import build_business_application
from scat.services.flight_service import ScatFlightService
from scat_mcp.server import create_server


async def mcp_call(application, context, name, arguments):
    server = create_server(application, lambda: context, synthetic=True)
    client_send, server_read = anyio.create_memory_object_stream(8)
    server_send, client_read = anyio.create_memory_object_stream(8)
    async with anyio.create_task_group() as group:
        group.start_soon(
            server.run, server_read, server_send, server.create_initialization_options()
        )
        async with ClientSession(client_read, client_send) as session:
            await session.initialize()
            response = await session.call_tool(name, arguments)
            result = json.loads(response.content[0].text)
        group.cancel_scope.cancel()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,empty", [("search", False), ("search", True), ("track", False), ("track", True)]
)
async def test_same_repository_values_through_all_three_entrypoints(kind, empty, monkeypatch):
    from agents import scat_tools as legacy
    from core import settings
    from service.scat_api import get_scat_service, router

    repository = Mock()
    flight = {
        "id": "SYNTH-DB",
        "source": {"batch": "SYNTH-batch", "external_id": "SYNTH-001"},
        "identity": {"callsign": "SAS406", "aircraft_type": "A320"},
        "plots": [],
        "total_points": 7,
        "start": 0,
    }
    repository.count_flights = AsyncMock(return_value=0 if empty else 1)
    repository.search_flights = AsyncMock(return_value=[] if empty else [flight])
    track = dict(
        flight,
        plots=[]
        if empty
        else [
            {
                "time_of_track": f"2016-10-20T20:00:0{i}",
                "I062/105": {"lat": 59 + i * 0.01, "lon": 18 + i * 0.01},
                "I062/136": {"measured_flight_level": 100 + i},
            }
            for i in range(5)
        ],
    )
    repository.get_track_points = AsyncMock(return_value=track)
    service = ScatFlightService(repository=repository)
    monkeypatch.setattr(legacy, "flight_service", service)
    monkeypatch.setattr(legacy.flight_database, "connect", AsyncMock())
    monkeypatch.setattr("scat.services.flight_service.ScatFlightService", lambda: service)
    application = build_business_application()
    context = ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID, "parity-thread")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_scat_service] = lambda: service
    if kind == "search":
        tool = "search_scat_flights"
        arguments = {"batch": "SYNTH-batch", "callsign": "sas406", "limit": 5}
        path = "/api/v1/scat/flights"
        query = dict(arguments)
    else:
        tool = "get_scat_track_points"
        arguments = {
            "batch": "SYNTH-batch",
            "external_id": "SYNTH-001",
            "limit": 5,
            "every_nth": 2,
            "fields": ["position"],
        }
        path = "/api/v1/scat/flights/SYNTH-batch/SYNTH-001/trajectory"
        query = {"limit": 5, "every_nth": 2, "fields": "position"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://parity"
    ) as client:
        response = await client.get(path, params=query)
    assert response.status_code == 200, response.text
    old = await getattr(legacy, tool).ainvoke(arguments)
    new = await mcp_call(application, context, tool, arguments)
    assert old["status"] == new["status"] == "ok"
    bodies = [response.json(), old["data"], new["data"]]
    if kind == "search":
        assert [bodies[0]["pagination"]["total"], bodies[1]["total"], bodies[2]["total"]] == [
            0 if empty else 1
        ] * 3
        assert [bodies[0]["pagination"]["returned"], bodies[1]["count"], bodies[2]["count"]] == [
            0 if empty else 1
        ] * 3
        assert [[v["identity"]["callsign"] for v in b["items"]] for b in bodies] == [
            [] if empty else ["SAS406"]
        ] * 3
        assert all(
            call.kwargs["callsign"] == "SAS406" for call in repository.count_flights.await_args_list
        )
    else:
        assert [b["pagination"] for b in bodies] == [bodies[0]["pagination"]] * 3
        expected = [] if empty else [0, 2, 4]
        assert [[v["sequence"] for v in b["points"]] for b in bodies] == [expected] * 3

        def utc(text):
            value = datetime.fromisoformat(text)
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

        # REST explicitly serializes UTC, while the shared service retains the source UTC text.
        assert [[utc(v["timestamp"]) for v in b["points"]] for b in bodies] == [
            [utc(v["timestamp"]) for v in bodies[0]["points"]]
        ] * 3
        assert [[v["position"]["lat_deg"] for v in b["points"]] for b in bodies] == [
            [] if empty else [59, 59.02, 59.04]
        ] * 3
        assert [[v["position"]["altitude_m"] for v in b["points"]] for b in bodies] == [
            [] if empty else [3048, 3108.96, 3169.92]
        ] * 3
        assert all(
            set(v) <= {"sequence", "timestamp", "position"} for b in bodies for v in b["points"]
        )
