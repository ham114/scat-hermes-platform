"""Exercise the trusted maintenance CLI against owned synthetic trace state."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from hermes_runtime.skill_evaluation import offline_bridge
    from hermes_runtime.skill_policy import SkillPolicy
    from hermes_runtime.skills import SkillRelease
    from hermes_runtime.trace_lifecycle import TraceLifecycle

    identity = uuid4().hex[:10]
    work = ROOT / ".tmp/s6" / ("maintenance-" + identity)
    work.mkdir(parents=True)
    policy = SkillPolicy(trace={"max_hot_records": 2})
    bridge = offline_bridge(
        SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json"), work, policy
    )
    report = {"status": "running", "real_model": False, "business_effects": 0, "checks": []}

    def call(action, *args, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/manage_skill_traces.py"),
                "--state",
                bridge.trace_path.relative_to(ROOT).as_posix(),
                "--action",
                action,
                *args,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        report["checks"].append(
            {"action": action, "expected_exit": expected, "exit": result.returncode}
        )
        assert result.returncode == expected, "maintenance_cli_unexpected_exit"
        return json.loads(result.stdout) if result.returncode == 0 else None

    try:
        for run in ("first", "second", "third"):
            bridge.bind("synthetic-session", "synthetic-owner", run)
            result = json.loads(
                bridge.handler("skill_view")(
                    {"name": "scat-flight-query"}, session_id="synthetic-session"
                )
            )
            assert result["success"]
            bridge.finish_session("synthetic-session")
        call("maintain")
        archived = call("read", "--scope", "synthetic-owner", "--run", "first")
        assert archived and archived[0]["records"]["skill_loads"]
        assert call("read", "--scope", "other-owner", "--run", "first") == []
        call("hold", "--scope", "synthetic-owner", "--run", "third")
        call("release", "--scope", "synthetic-owner", "--run", "third")
        candidate = SkillPolicy(
            trace={"max_hot_records": 2, "hot_retention_days": 1, "archive_retention_days": 2}
        )
        path = work / "candidate.json"
        path.write_text(candidate.model_dump_json())
        arguments = ["--candidate-policy", path.relative_to(ROOT).as_posix()]
        preview = call("preview", *arguments)
        call("apply", *arguments, "--preview-sha256", "invalid", expected=1)
        call("apply", *arguments, "--preview-sha256", preview["preview_sha256"])
        assert TraceLifecycle(bridge.trace_path, candidate, workspace_root=ROOT).policy == candidate
        report.update(
            status="passed",
            archive_integrity_and_scope=True,
            preview_required=True,
            acknowledged_retention_applied=True,
        )
    finally:
        assert work.resolve().parent == (ROOT / ".tmp/s6").resolve() and not work.is_symlink()
        shutil.rmtree(work)
        report["temporary_workspace_removed"] = True
        path = ROOT / "docs/validation" / ("20261023-S6-maintenance-cli-" + identity + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": path.relative_to(ROOT).as_posix(), "status": report["status"]}))


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda Python required")
    main()
