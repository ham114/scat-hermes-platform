"""Run inside the deployed native image: isolated dispatch, synthetic handler, no LLM."""

import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

root = Path("/home/h3c/project/python/scat-hermes-platform")
home = root / ".tmp" / ("query-display-dispatch-" + uuid4().hex)
home.mkdir(exist_ok=False)
try:
    os.environ.update(
        HERMES_HOME=str(home),
        HERMES_DISABLE_LAZY_INSTALLS="1",
        TMPDIR=str(home),
        TEMP=str(home),
        TMP=str(home),
    )
    (home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": []}}))
    sys.path.insert(0, str(root / "vendor/hermes-agent"))
    from model_tools import handle_function_call
    from tools.registry import registry

    from hermes_runtime.skill_guard import SkillOperationGuard
    from hermes_runtime.skills import SkillRelease, register_native_skills

    release = SkillRelease(root, root / "services/hermes/skills.lock.json")
    bridge = register_native_skills(
        release, home, root / "vendor/hermes-agent", home / "trace.sqlite"
    )
    bridge.bind("probe-session", "probe-scope", "probe-run")
    operation = "mcp__scat__get_scat_flight_overview"
    calls = []

    def backend(args, **kwargs):
        calls.append(True)
        return json.dumps({"status": "ok", "data": {}, "synthetic": True})

    registry.register(
        name=operation,
        toolset="query-display-probe",
        schema={
            "name": operation,
            "description": "Synthetic dispatch check",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=backend,
    )
    SkillOperationGuard(bridge, {operation}, set()).install(registry)

    def invoke(name, args):
        return json.loads(handle_function_call(name, args, session_id="probe-session"))

    assert invoke("skill_view", {"name": "scat-trajectory-analysis"})["success"]
    denied = invoke(operation, {})
    assert denied["error"] == "skill_operation_not_allowed" and not calls
    assert invoke("skill_view", {"name": "scat-flight-query"})["success"]
    assert invoke(operation, {})["status"] == "ok" and len(calls) == 1
    bridge.bind("probe-session", "probe-scope", "probe-next-run")
    again = invoke(operation, {})
    assert again["error"] == "skill_required" and len(calls) == 1
    print(
        json.dumps(
            {
                "dispatch_checks": "passed",
                "synthetic_handler_calls": len(calls),
                "wrong_skill_denied": True,
                "explicit_supplement_succeeds": True,
                "cross_run_requires_reload": True,
                "denial_contains_recovery": bool(denied.get("recovery")),
                "new_run_denial_contains_recovery": bool(again.get("recovery")),
                "bundle_version": release.manifest.get("bundle_version")
                if hasattr(release, "manifest")
                else None,
                "llm_calls": 0,
            }
        )
    )
finally:
    assert home.parent == root / ".tmp" and home.name.startswith("query-display-dispatch-")
    shutil.rmtree(home)
