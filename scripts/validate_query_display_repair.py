"""Server-only repair checks, without LLM calls or production changes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
WORK = ROOT / ".tmp/availability-repair"
OUT = ROOT / "var/validation/20261107-query-display-repair"


def native():
    home = WORK / "native-dispatch"
    home.mkdir(parents=True, exist_ok=False)
    try:
        os.environ.update(
            HERMES_HOME=str(home),
            HERMES_DISABLE_LAZY_INSTALLS="1",
            TMPDIR=str(home),
            TMP=str(home),
            TEMP=str(home),
        )
        (home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": []}}))
        sys.path.insert(0, str(ROOT / "vendor/hermes-agent"))
        from model_tools import handle_function_call
        from tools.registry import registry

        from hermes_runtime.skill_guard import SkillOperationGuard
        from hermes_runtime.skills import SkillRelease, register_native_skills

        release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
        bridge = register_native_skills(
            release, home, ROOT / "vendor/hermes-agent", home / "trace.sqlite"
        )
        bridge.bind("repair-session", "repair-scope", "repair-run")
        operation = "mcp__scat__get_scat_flight_overview"
        calls = []

        def backend(args, **kwargs):
            calls.append(True)
            return json.dumps({"status": "ok", "data": {}, "synthetic": True})

        registry.register(
            name=operation,
            toolset="repair-probe",
            schema={
                "name": operation,
                "description": "Synthetic repair check",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=backend,
        )
        SkillOperationGuard(bridge, {operation}, set()).install(registry)

        def invoke(name, args):
            return json.loads(handle_function_call(name, args, session_id="repair-session"))

        assert invoke("skill_view", {"name": "scat-trajectory-analysis"})["success"]
        denied = invoke(operation, {})
        assert denied["error"] == "skill_operation_not_allowed" and not calls
        hint = denied["recovery"]["skills"]
        assert [item["name"] for item in hint] == ["scat-flight-query"]
        assert invoke("skill_view", {"name": hint[0]["name"]})["success"]
        assert invoke(operation, {})["status"] == "ok" and len(calls) == 1
        bridge.bind("repair-session", "repair-scope", "repair-next-run")
        assert invoke(operation, {})["error"] == "skill_required" and len(calls) == 1
        return {
            "status": "passed",
            "native_dispatch": True,
            "recovery_then_explicit_load": True,
            "cross_run_still_denied": True,
            "synthetic_backend_calls": len(calls),
            "bundle_sha256": release.bundle_sha256,
            "llm_calls": 0,
        }
    finally:
        shutil.rmtree(home)


async def business():
    from application.context import ExecutionContext
    from application.readonly_display import receipt_display
    from application.readonly_extensions import build_extended_business_application
    from core.settings import settings
    from hermes_runtime.results import ToolResultStore

    home = WORK / "business-display"
    home.mkdir(parents=True, exist_ok=False)
    app = build_extended_business_application()
    context = ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID, "query-display-repair")
    try:
        # User's known flight, with the schema's existing deployment batch default.
        args = {"external_id": "100002", "limit": 100, "fields": ["position", "motion"]}
        raw = await app.invoke("get_scat_track_points", args, context=context)
        assert raw["status"] == "ok", raw["status"]
        assert raw.get("representation") != "page_summary"
        store = ToolResultStore(home / "results.sqlite", workspace_root=ROOT)
        ref = store.put(context.scope_key, "run_repair", "track", "get_scat_track_points", raw)
        receipt = store.get(context.scope_key, "run_repair", ref["result_id"])
        displayed = receipt_display(receipt)
        analysis = displayed["analysis_result"]
        trajectory = next(b for b in analysis["blocks"] if b["type"] == "trajectory_2d")
        assert len(trajectory["points"]) == len(raw["data"]["points"]) > 1
        assert (
            trajectory["points"][0]["longitude"] == raw["data"]["points"][0]["position"]["lon_deg"]
        )
        assert displayed["result"] == raw and "analysis_result" not in receipt
        return {
            "status": "passed",
            "real_database": True,
            "llm_calls": 0,
            "prediction_calls": 0,
            "raw_data_exported": False,
            "points": len(trajectory["points"]),
            "block_types": sorted({b["type"] for b in analysis["blocks"]}),
            "scope": "fetched_page",
            "has_more": raw["data"]["pagination"]["has_more"],
            "source_preserved": True,
            "coordinate_equality_checked": True,
        }
    finally:
        await app.aclose()
        shutil.rmtree(home)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["native", "business"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        report = native() if args.mode == "native" else asyncio.run(business())
    except Exception as exc:  # noqa: BLE001 -- persist a sanitized failure without raw data.
        report = {"status": "failed", "error_type": type(exc).__name__, "llm_calls": 0}
    (OUT / (args.mode + "-check.json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
