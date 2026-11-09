"""Native dispatch acceptance for S2 admission; no LLM, DB or model execution."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(report):
    if sys.version_info[:2] != (3, 14) or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("dedicated_hermes_conda_required")
    home = ROOT / ".tmp/s2b" / ("native-" + uuid4().hex[:12])
    home.mkdir(parents=True)

    def cleanup_home():
        assert home.resolve().is_relative_to(ROOT.resolve()) and not home.is_symlink()
        shutil.rmtree(home, ignore_errors=True)

    # Register before native imports: their exit callbacks may recreate cache directories.
    atexit.register(cleanup_home)
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
            release, home, ROOT / "vendor/hermes-agent", home / "skill-loads.sqlite"
        )
        bridge.bind("s2-native-session", "s2-owner-thread", "s2-run-1")
        query = "mcp__scat__search_scat_flights"
        calls = []
        forbidden_calls = []

        def forbid(*args, **kwargs):
            forbidden_calls.append(True)
            raise AssertionError("unexpected_install_or_preprocess")

        bridge.native._preprocess_skill = forbid
        import pm

        pm.ensure = forbid

        def register_query(marker):
            def handler(args, **kwargs):
                calls.append(marker)
                return json.dumps({"status": "ok", "synthetic": True, "marker": marker})

            registry.register(
                name=query,
                toolset="s2-admission-probe",
                schema={
                    "name": query,
                    "description": "Synthetic probe",
                    "parameters": {"type": "object", "properties": {}},
                },
                handler=handler,
            )

        register_query("initial")
        guard = SkillOperationGuard(bridge, {query, "scat_task_prepare"}, set())
        guard.install(registry)

        def invoke(name, args, session="s2-native-session"):
            return json.loads(handle_function_call(name, args, session_id=session))

        assert invoke(query, {})["error"] == "skill_required"
        assert invoke(
            "skill_view", {"name": "scat-flight-query", "file_path": "references/query-contract.md"}
        )["success"]
        assert invoke(query, {})["error"] == "skill_required"
        assert invoke("skill_view", {"name": "scat-investigation"})["success"]
        assert invoke(query, {})["error"] == "skill_operation_not_allowed"
        assert invoke("skill_view", {"name": "aviation-knowledge"})["success"]
        assert invoke(query, {})["error"] == "skill_operation_not_allowed"
        assert not calls
        assert invoke("skill_view", {"name": "scat-flight-query"})["success"]
        assert invoke(query, {})["marker"] == "initial"
        assert invoke(query, {}, session="forged")["error"] == "trusted_skill_context_required"
        bridge.bind("s2-native-session", "s2-owner-thread", "s2-run-2")
        register_query("refreshed")
        assert invoke(query, {})["error"] == "skill_required"
        assert invoke("skill_view", {"name": "scat-flight-query"})["success"]
        assert invoke(query, {})["marker"] == "refreshed"
        assert invoke("scat_task_prepare", {})["error"] == "skill_operation_not_allowed"
        guard.verify_installed()
        with sqlite3.connect(bridge.trace_path) as db:
            rows = db.execute(
                "SELECT run_id,operation,skill_id,outcome,load_id FROM skill_operations ORDER BY id"
            ).fetchall()
        assert [row[0] for row in rows] == ["s2-run-1", "s2-run-2"]
        assert len(calls) == 2 and not forbidden_calls
        report.update(
            status="passed",
            native_dispatch=True,
            handler_refresh_guarded=True,
            backend_calls=len(calls),
            rejected_without_backend_calls=7,
            operation_receipts=rows,
            preprocess_calls=0,
            dependency_install_calls=0,
            real_model=False,
            real_database_query=False,
            synthetic=True,
            bundle_sha256=release.bundle_sha256,
        )
    finally:
        assert home.resolve().is_relative_to(ROOT.resolve())
        cleanup_home()
        report["temporary_home_removed"] = not home.exists()


if __name__ == "__main__":
    report = {"status": "running"}
    try:
        main(report)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        path = ROOT / "docs/validation" / ("20261023-S2B-native-" + uuid4().hex[:8] + ".json")
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
