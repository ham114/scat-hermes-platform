"""Deterministic S2 scenario harness. This does not score model intent selection."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from application.context import ExecutionContext
from application.readonly import ReadOnlyApplication
from application.readonly_extensions import ALL_READ_ONLY_TOOLS
from hermes_runtime.readonly_contracts import resolve_reference, result_reference
from hermes_runtime.results import ToolResultStore, publish_tool_result
from hermes_runtime.skill_guard import SkillOperationGuard
from hermes_runtime.skills import NativeSkillBridge, SkillRelease


def offline_bridge(release, home, policy=None):
    folder = release.stage(home)
    native = SimpleNamespace(
        skill_search_dirs=lambda: ([(1, folder)], folder),
        skills_list=lambda **kw: json.dumps(
            {"success": True, "skills": [{"name": n} for n in release.entries]}
        ),
        skill_view=lambda name, file_path=None, **kw: json.dumps(
            {
                "success": True,
                "content": (folder / name / (file_path or "SKILL.md")).read_text(encoding="utf-8"),
            }
        ),
    )
    return NativeSkillBridge(release, folder, native, home / "trace.sqlite", policy=policy)


def evaluate_cases(root, work):
    release = SkillRelease(root, root / "services/hermes/skills.lock.json")
    suite = json.loads((root / "evaluations/hermes_s2_v1.json").read_text(encoding="utf-8"))
    bridge = offline_bridge(release, work)
    guard = SkillOperationGuard(bridge, {"mcp__scat__" + n for n in ALL_READ_ONLY_TOOLS}, set())
    store = ToolResultStore(work / "results.sqlite", workspace_root=root)
    rows = []
    for case in suite["cases"]:
        calls = []
        context = ExecutionContext("scat-researcher", case["id"])
        run_id = "run_" + case["id"]
        bridge.bind(case["id"], context.scope_key, run_id)

        async def backend(_calls=calls, _case=case, **kwargs):
            _calls.append(kwargs)
            if _case["mode"] == "fault":
                raise RuntimeError("synthetic service fault")
            if _case["mode"] == "empty":
                return {"status": "not_found", "data": None}
            return {
                "status": "ok",
                "synthetic": True,
                "query": kwargs,
                "data": {"count": 1, "value": 7, "unit": "synthetic"},
            }

        app = ReadOnlyApplication({case["operation"]: backend}, specs=ALL_READ_ONLY_TOOLS)
        if case["mode"] != "forbidden":
            loaded = json.loads(
                bridge.handler("skill_view")({"name": case["skill"]}, session_id=case["id"])
            )
            assert loaded["success"]

        def dispatch(
            name, args, _app=app, _operation=case["operation"], _context=context, **kwargs
        ):
            return json.dumps(asyncio.run(_app.invoke(_operation, args, context=_context)))

        tool = "mcp__scat__" + case["operation"]
        payload = json.loads(guard.invoke(dispatch, tool, case["arguments"], session_id=case["id"]))
        observed = payload.get("error") if case["mode"] == "forbidden" else payload.get("status")
        passed = observed == case["expected"] and len(calls) == case["expected_backend_calls"]
        reference = publish_tool_result(
            store,
            scope_key=context.scope_key,
            run_id=run_id,
            call_id=case["id"],
            tool=tool,
            result=payload,
        )
        if observed == "ok":
            passed = (
                passed
                and payload["data"]["value"] == 7
                and payload["data"]["unit"] == "synthetic"
                and reference is not None
            )
            typed = result_reference(
                store, release, context.scope_key, run_id, reference["result_id"]
            )
            resolved = resolve_reference(
                store,
                release,
                context.scope_key,
                run_id,
                typed.model_dump(),
                expected_contract=typed.contract,
            )
            passed = passed and resolved == payload
        else:
            passed = passed and reference is None
        rows.append(
            {
                "id": case["id"],
                "domain": case["domain"],
                "mode": case["mode"],
                "passed": bool(passed),
                "observed": observed,
                "backend_calls": len(calls),
            }
        )
        bridge.finish_session(case["id"])
    domains = {
        name: {
            "passed": sum(row["passed"] for row in rows if row["domain"] == name),
            "total": sum(row["domain"] == name for row in rows),
        }
        for name in sorted({row["domain"] for row in rows})
    }
    return {
        "status": "passed" if all(row["passed"] for row in rows) else "failed",
        "kind": "deterministic contract scenarios; not model selection accuracy",
        "real_model": False,
        "real_database_query": False,
        "domains": domains,
        "cases": rows,
        "deferred_domains": suite["deferred_domains"],
        "bundle_sha256": release.bundle_sha256,
    }
