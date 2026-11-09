"""Read real flight/track data on-server only; no LLM client or external API calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.settings import settings
from scat.infrastructure.database import flight_database
from scat.schemas.tool_inputs import DEFAULT_BATCH


async def probe(report):
    # The existing database and MCP are read in this server process only.
    try:
        await flight_database.connect()
        count = await flight_database.get_collection(
            settings.SCAT_FLIGHTS_COLLECTION
        ).count_documents({"source.batch": DEFAULT_BATCH}, maxTimeMS=5000)
    finally:
        await flight_database.close()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(ROOT / "scripts/run_scat_mcp.py")],
        cwd=str(ROOT),
        env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        result = await asyncio.wait_for(
            client.call_tool("search_scat_flights", {"batch": DEFAULT_BATCH, "limit": 1}),
            timeout=45,
        )
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "ok" and payload["data"]["total"] == count
        items = payload["data"]["items"]
        assert len(items) == 1
        result = await asyncio.wait_for(
            client.call_tool(
                "get_scat_track_points",
                {
                    "batch": DEFAULT_BATCH,
                    "external_id": str(items[0]["source"]["external_id"]),
                    "limit": 1,
                },
            ),
            timeout=45,
        )
        track = json.loads(result.content[0].text)
        assert track["status"] == "ok" and len(track["data"]["points"]) == 1
        report.update(
            status="passed",
            database_total_matches=True,
            one_flight_read=True,
            one_track_point_read=True,
            llm_calls=0,
            external_data_transfer=False,
            raw_business_data_in_report=False,
        )


if __name__ == "__main__":
    report = {"status": "running"}
    try:
        asyncio.run(probe(report))
    except (OSError, ValueError, KeyError, AssertionError, TimeoutError) as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    (ROOT / "docs/validation/20261023-S1-business-local.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
