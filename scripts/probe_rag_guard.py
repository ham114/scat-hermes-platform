"""Exercise real native skill dispatch for the opt-in RAG tool without model calls."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-hermes/bin/python":
        raise ValueError("use_existing_scat_hermes")
    home = ROOT / ".tmp/rag-06b" / ("guard-" + uuid4().hex)
    home.mkdir(parents=True)
    report = {"status": "running", "model_calls": 0, "checks": {}}
    try:
        os.environ.update(
            HERMES_HOME=str(home),
            HERMES_DISABLE_LAZY_INSTALLS="1",
            TMPDIR=str(home),
            TMP=str(home),
            TEMP=str(home),
        )
        (home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": []}}))
        sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor/hermes-agent")]
        from model_tools import handle_function_call
        from tools.registry import registry

        from hermes_runtime.rag_operation import TOOL, register_knowledge_rag
        from hermes_runtime.skill_guard import SkillOperationGuard
        from hermes_runtime.skills import SkillRelease, register_native_skills

        release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
        bridge = register_native_skills(
            release, home, ROOT / "vendor/hermes-agent", home / "skills.sqlite"
        )
        bridge.bind("rag-guard", "rag-guard-owner", "rag-guard-run")
        backend_calls = []

        def legacy(arguments, **kwargs):
            backend_calls.append(arguments)
            return json.dumps({"status": "ok", "synthetic": True, "legacy": arguments})

        registry.register(
            name=TOOL,
            toolset="scat",
            schema={
                "name": TOOL,
                "description": "Guard acceptance fixture",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            handler=legacy,
        )
        guard = SkillOperationGuard(bridge, {TOOL}, set())
        guard.install(registry)
        state = ROOT / "var/validation/20261026-rag-06b/guard-state"
        operation = register_knowledge_rag(
            registry, bridge, ROOT, state, ROOT / "services/rag/real-agent-06b-service.json"
        )

        def forbid(*args):
            raise AssertionError("unexpected_rag_model_execution")

        operation.executor = forbid

        def invoke(name, args, session="rag-guard"):
            return json.loads(handle_function_call(name, args, session_id=session))

        checks = report["checks"]
        checks["unloaded_deep_rejected"] = (
            invoke(TOOL, {"query": "test", "mode": "deep"})["error"] == "skill_required"
        )
        checks["actual_skill_body_loaded"] = invoke("skill_view", {"name": "aviation-knowledge"})[
            "success"
        ]
        checks["default_fast_original_handler"] = invoke(TOOL, {"query": "test"}).get("legacy") == {
            "query": "test"
        }
        checks["explicit_fast_strips_mode"] = invoke(TOOL, {"query": "test", "mode": "fast"}).get(
            "legacy"
        ) == {"query": "test"}
        checks["forged_session_rejected"] = (
            invoke(TOOL, {"query": "test", "mode": "deep"}, "forged")["error"]
            == "trusted_skill_context_required"
        )
        checks["model_budget_override_rejected"] = (
            invoke(TOOL, {"query": "test", "mode": "deep", "scope": "forged"})["status"]
            == "invalid_request"
        )
        checks["missing_parent_budget_rejected"] = (
            invoke(TOOL, {"query": "test", "mode": "deep"})["status"] == "forbidden"
        )
        checks["only_fast_reached_legacy"] = len(backend_calls) == 2
        guard.verify_installed()
        with sqlite3.connect(operation.path) as db:
            checks["no_deep_process_started"] = (
                db.execute("SELECT COUNT(*) FROM rag_invocations").fetchone()[0] == 0
            )
        report["status"] = "passed" if all(checks.values()) else "failed"
    finally:
        if (
            home.resolve().is_relative_to((ROOT / ".tmp/rag-06b").resolve())
            and not home.is_symlink()
        ):
            shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()
        (ROOT / "docs/validation/20261026-rag-06b-guard.json").write_text(
            json.dumps(report, indent=2)
        )
        print(json.dumps(report))


if __name__ == "__main__":
    main()
