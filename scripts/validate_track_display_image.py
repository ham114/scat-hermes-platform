"""Read existing production receipts through the candidate API in a networkless container.

No initialization, writes, HTTP listener, model calls or trajectory exports.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException

from application.context import ExecutionContext
from hermes_runtime.registry import RunRegistry
from hermes_runtime.results import ToolResultStore
from service.hermes_api import get_result

ROOT = Path.cwd()
THREAD = "3a298bec-bafd-48bd-b99c-509f295dbd1f"
RUN = "run_63a16cd882dd4b33bfd260959c1e12c8"


def readonly(path):
    db = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


async def main():
    directory = ROOT / "var/deploy/h7b/shared/state"
    # Bypass constructors that initialize schemas; retain their exact read/owner/hash checks.
    registry = RunRegistry.__new__(RunRegistry)
    registry._connect = lambda: readonly(directory / "bindings.sqlite")
    results = ToolResultStore.__new__(ToolResultStore)
    results._connect = lambda: readonly(directory / "results.sqlite")
    runtime = SimpleNamespace(registry=registry, results=results)
    context = ExecutionContext("scat-researcher", THREAD)
    refs = results.list(context.scope_key, RUN)
    pages = []
    for ref in refs:
        if ref["operation"] != "get_scat_track_points":
            continue
        projected = await get_result(
            UUID(THREAD), RUN, ref["result_id"], runtime, "scat-researcher"
        )
        analysis = projected["analysis_result"]
        assert analysis["context"]["readonly_payload_sha256"] == ref["payload_sha256"]
        trajectory = next(b for b in analysis["blocks"] if b["type"] == "trajectory_2d")
        assert len(trajectory["points"]) == len(projected["result"]["data"]["points"])
        assert (
            results.get(context.scope_key, RUN, ref["result_id"])["result"] == projected["result"]
        )
        pages.append(
            {
                "start": projected["result"]["data"]["pagination"]["start"],
                "points": len(trajectory["points"]),
                "source_hash_preserved": True,
            }
        )
    assert len(pages) == 6 and sum(p["points"] for p in pages) == 524
    try:
        await get_result(UUID(THREAD), RUN, refs[0]["result_id"], runtime, "other-owner")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("cross_owner_was_not_denied")
    return {
        "status": "passed",
        "candidate_api_readback": True,
        "pages": pages,
        "cross_owner_denied": True,
        "production_database_writes": 0,
        "raw_data_exported": False,
        "new_model_calls": 0,
        "services_restarted": False,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
