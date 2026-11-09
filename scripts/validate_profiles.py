"""Server-only finite backend regression; no frontend, installs or new environments."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
NATIVE = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def main():
    if sys.executable != APP or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing server scat-agent Conda Python")
    identity = uuid4().hex[:10]
    work = ROOT / ".tmp/profiles" / ("regression-" + identity)
    work.mkdir(parents=True)
    env = os.environ | {
        "TMPDIR": str(work),
        "TMP": str(work),
        "TEMP": str(work),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUFF_CACHE_DIR": str(work / "ruff"),
    }
    paths = [
        "src/hermes_runtime/profiles.py",
        "src/hermes_runtime/native_profiles.py",
        "src/hermes_runtime/author_bridge.py",
        "scripts/profile_manage.py",
        "scripts/probe_native_profiles.py",
        "scripts/probe_profiles_runtime.py",
        "scripts/run_profiles_with_dns_cache.py",
        "scripts/validate_profiles.py",
        "tests/hermes_runtime/test_standard_profiles.py",
        "src/application/skill_worker.py",
        "src/hermes_runtime/release_store.py",
        "src/hermes_runtime/release_bridge.py",
        "src/hermes_runtime/authoring_store.py",
        "src/hermes_runtime/author_trials.py",
        "src/service/skill_authoring_api.py",
        "src/hermes_runtime/job_context.py",
        "src/application/workflow_operations.py",
        "scripts/run_skill_author_api.py",
        "scripts/run_skill_author_trial.py",
        "scripts/probe_authoring_s7.py",
        "scripts/probe_native_releases_s7.py",
        "scripts/probe_author_recovery_s7.py",
        "tests/hermes_runtime/test_authoring_s7.py",
        "src/hermes_runtime/trace_lifecycle.py",
        "src/hermes_runtime/trace_references.py",
        "src/hermes_runtime/skill_cache.py",
        "src/hermes_runtime/entrypoint.py",
        "src/hermes_runtime/stability.py",
        "src/hermes_runtime/skills.py",
        "src/hermes_runtime/skill_guard.py",
        "src/hermes_runtime/skill_policy.py",
        "src/core/settings.py",
        "src/application/skill_job_factory.py",
        "src/application/skill_jobs.py",
        "src/application/workflow_tasks.py",
        "scripts/run_hermes_api.py",
        "scripts/manage_skill_traces.py",
        "scripts/evaluate_skills_s6.py",
        "scripts/run_synthetic_s6_with_dns_cache.py",
        "scripts/validate_profiles.py",
        "tests/hermes_runtime/test_lifecycle_s6.py",
        "tests/hermes_runtime/test_stability_s6.py",
    ]
    checks = [
        ("lint", [APP, "-B", "-m", "ruff", "check", *paths], 60),
        ("format", [APP, "-B", "-m", "ruff", "format", "--check", *paths], 60),
        (
            "native-imports",
            [
                NATIVE,
                "-B",
                "-c",
                "import sys;sys.path.insert(0,'src');from hermes_runtime.trace_lifecycle import TraceLifecycle;from hermes_runtime.skill_cache import SkillCatalogCache;from hermes_runtime.workflow_bridge import WorkflowBridge;assert 'numpy' not in sys.modules;assert 'service.hermes_app' not in sys.modules;print('native isolation passed')",
            ],
            60,
        ),
        (
            "pytest",
            [
                APP,
                "-B",
                "-m",
                "pytest",
                "tests",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp=" + str(work / "pytest"),
                "--junitxml="
                + str(ROOT / "var/validation" / ("20261024-profiles-" + identity + "-pytest.xml")),
            ],
            900,
        ),
        ("app-pip-check", [APP, "-B", "-m", "pip", "check"], 60),
        ("native-pip-check", [NATIVE, "-B", "-m", "pip", "check"], 60),
    ]
    report = {
        "status": "running",
        "identity": identity,
        "frontend_modified": False,
        "new_dependencies": False,
        "checks": [],
    }
    try:
        for name, command, timeout in checks:
            start = time.monotonic()
            path = ROOT / "var/validation" / ("20261024-profiles-" + identity + "-" + name + ".log")
            with path.open("x") as log:
                path.chmod(0o600)
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    timeout=timeout,
                    check=False,
                )
            row = {
                "name": name,
                "exit": result.returncode,
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "log": path.relative_to(ROOT).as_posix(),
            }
            report["checks"].append(row)
            print(json.dumps(row), flush=True)
            if result.returncode:
                break
        report["status"] = (
            "passed"
            if len(report["checks"]) == len(checks)
            and all(r["exit"] == 0 for r in report["checks"])
            else "failed"
        )
    finally:
        assert work.resolve().parent == (ROOT / ".tmp/profiles").resolve() and not work.is_symlink()
        shutil.rmtree(work)
        report["temporary_workspace_removed"] = True
        path = ROOT / "docs/validation" / ("20261024-profiles-" + identity + "-backend-driver.json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps({"report": path.relative_to(ROOT).as_posix(), "status": report["status"]}),
            flush=True,
        )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
