"""Read-only real-data MCP checks; reports metadata, never credentials or raw tracks."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.settings import settings
from scat.infrastructure.database import flight_database

REPORT = ROOT / "var/validation/h3-real-business.json"
BATCH = "scat20161015_20161021"


def save(report):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    REPORT.chmod(0o600)


async def main(report):
    count = None
    try:
        await flight_database.connect()
        count = await flight_database.get_collection(
            settings.SCAT_FLIGHTS_COLLECTION
        ).count_documents({"source.batch": BATCH}, maxTimeMS=5000)
        report["mongodb"] = {"connected": True, "batch_count": count}
    except Exception as exc:  # noqa: BLE001 -- report only a safe failure classification.
        report["mongodb"] = {"connected": False, "error_type": type(exc).__name__}
    finally:
        await flight_database.close()
    save(report)
    temp = ROOT / ".tmp/h4/mcp"
    temp.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(ROOT / "scripts/run_scat_mcp.py")],
        cwd=str(ROOT),
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
            "SCAT_RAG_BASELINE_ENABLED": "true",
            "SCAT_RAG_BASELINE_URL": "http://127.0.0.1:8091",
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
        },
    )
    report["operations"] = {}
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        info = await client.initialize()
        listing = await client.list_tools()
        report.update(mcp_server=info.serverInfo.name, tool_count=len(listing.tools))

        async def call(name, args):
            started = time.monotonic()
            result = await asyncio.wait_for(client.call_tool(name, args), timeout=75)
            payload = json.loads(result.content[0].text)
            metadata = {
                "status": payload.get("status"),
                "seconds": round(time.monotonic() - started, 2),
            }
            report["operations"][name] = metadata
            save(report)
            return payload, metadata

        flights, metadata = await call("search_scat_flights", {"batch": BATCH, "limit": 1})
        data = flights.get("data") or {}
        metadata.update(
            returned_count=data.get("count"),
            total=data.get("total"),
            database_count_matches=count is not None and data.get("total") == count,
        )
        items = data.get("items") or []
        if items:
            external_id = str(items[0]["source"]["external_id"])
            track, meta = await call(
                "get_scat_track_points", {"batch": BATCH, "external_id": external_id, "limit": 3}
            )
            meta["result_fields"] = sorted((track.get("data") or {}).keys())
            track_data = track.get("data") or {}
            points = track_data.get("points") or []
            returned = (track_data.get("pagination") or {}).get("returned_points")
            meta["returned_points"] = returned
            meta["bounded"] = returned == len(points) and 0 < len(points) <= 3
        else:
            report["operations"]["get_scat_track_points"] = {"status": "no_reference_flight"}
        overview, meta = await call(
            "get_scat_batch_overview", {"batch": BATCH, "distribution_limit": 2}
        )
        meta["result_fields"] = sorted((overview.get("data") or {}).keys())
        quality, meta = await call("inspect_scat_collection_data_quality", {"batch": BATCH})
        meta["result_fields"] = sorted((quality.get("data") or {}).keys())
        knowledge, meta = await call(
            "search_scat_knowledge", {"query": "ADE 和 FDE 的含义与单位是什么？", "limit": 2}
        )
        retrieval = knowledge.get("data") or {}
        results = retrieval.get("results") or []
        meta.update(
            retrieval_status=retrieval.get("status"),
            count=len(results),
            citations_present=bool(results)
            and all(
                all(hit.get(key) for key in ["source_id", "source_version", "chunk_id", "locator"])
                for hit in results
            ),
            source_ids=sorted({hit.get("source_id", "") for hit in results}),
        )
    report["mcp_closed"] = True
    report["status"] = (
        "passed"
        if (
            len(report["operations"]) == 5
            and all(item["status"] == "ok" for item in report["operations"].values())
            and metadata["database_count_matches"]
            and report["operations"]["get_scat_track_points"].get("bounded")
            and report["operations"]["search_scat_knowledge"].get("citations_present")
        )
        else "failed"
    )


if __name__ == "__main__":
    report = {"status": "running", "batch": BATCH, "started_at": time.time()}
    save(report)
    try:
        asyncio.run(main(report))
    except Exception as exc:  # noqa: BLE001 -- no exception text or credential may reach reports.
        report.update(status="failed", error_type=type(exc).__name__)
    report["finished_at"] = time.time()
    save(report)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)
