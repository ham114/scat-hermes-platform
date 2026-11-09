"""Actual business data stays inside this server; no LLM request."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path("/home/h3c/project/python/scat-hermes-platform").resolve()
assert Path.cwd().resolve() == ROOT
sys.path.insert(0, str(ROOT / "src"))


async def main():
    from application.context import ExecutionContext
    from application.readonly_extensions import build_extended_business_application
    from core.settings import settings

    app = build_extended_business_application()
    ctx = ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID, "s2b-server-only")
    report = {
        "status": "running",
        "real_model": False,
        "real_database_query": True,
        "raw_data_exported": False,
    }
    try:
        batches = await app.invoke("list_scat_batches", {"limit": 1}, context=ctx)
        assert batches["status"] == "ok"
        items = batches["data"]["items"]
        # Service returns batch rows with a batch key; never guess an identifier.
        first = items[0]
        batch = first.get("batch") or first.get("_id")
        assert isinstance(batch, str) and batch
        query = await app.invoke("search_scat_flights", {"batch": batch, "limit": 1}, context=ctx)
        assert query["status"] == "ok" and query["data"]["items"]
        flight = query["data"]["items"][0]
        external_id = flight["source"]["external_id"]
        track = await app.invoke(
            "get_scat_track_points",
            {"batch": batch, "external_id": external_id, "limit": 1},
            context=ctx,
        )
        assert track["status"] == "ok"
        models = await app.invoke("list_uav_models", {}, context=ctx)
        assert models["status"] == "ok"
        uav = await app.invoke("search_uav_flights", {"limit": 1}, context=ctx)
        assert uav["status"] == "ok" and uav["data"]["items"]
        uav_id = uav["data"]["items"][0]["external_id"]
        channels = await app.invoke("list_uav_channels", {}, context=ctx)
        assert channels["status"] == "ok" and channels["data"]["items"]
        channel = channels["data"]["items"][0]["key"]
        samples = await app.invoke(
            "get_uav_samples",
            {"external_id": uav_id, "channels": [channel], "limit": 1},
            context=ctx,
        )
        assert samples["status"] == "ok"
        report.update(
            status="passed",
            successful_read_operations=7,
            scat_flights_returned=len(query["data"]["items"]),
            scat_track_points_returned=len(track["data"]["points"]),
            uav_flights_returned=len(uav["data"]["items"]),
            uav_samples_returned=samples["data"]["returned"],
            uav_models_returned=len(models["data"]["items"]),
            quality_persistent_cache_enabled=False,
        )
        return report
    finally:
        await app.aclose()


if __name__ == "__main__":
    from uuid import uuid4

    report = asyncio.run(main())
    p = ROOT / "docs/validation" / ("20261023-S2C-business-" + uuid4().hex[:8] + ".json")
    assert not p.exists()
    p.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
