"""Finite S5 server regression driver; raw evidence survives failures."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    identity = uuid4().hex[:10]
    prefix = "20261023-S5-" + identity
    home = ROOT / ".tmp/s5" / ("regression-" + identity)
    home.mkdir(parents=True)
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT / "src"),
        "TMPDIR": str(home),
        "TMP": str(home),
        "TEMP": str(home),
    }
    python = str(Path(sys.executable).resolve())
    hermes = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"
    files = [
        "src/workflow_runtime",
        "src/application/workflow_tasks.py",
        "src/application/workflow_operations.py",
        "src/application/skill_jobs.py",
        "src/application/skill_job_store.py",
        "src/application/skill_worker.py",
        "src/application/skill_job_factory.py",
        "src/scat_model_runtime/contracts.py",
        "src/analysis_runs/repository.py",
        "src/hermes_runtime/workflow_bridge.py",
        "src/hermes_runtime/skill_guard.py",
        "src/hermes_runtime/job_context.py",
        "src/service/model_task_api.py",
        "src/service/workflow_api.py",
        "src/service/hermes_app.py",
        "src/core/settings.py",
        "src/scat/materialization/tasks.py",
        "scripts/run_workflow_backend.py",
        "scripts/run_workflow_tool.py",
        "scripts/run_hermes_api.py",
        "scripts/probe_workflows_s5.py",
        "scripts/run_synthetic_s5_with_dns_cache.py",
        "scripts/validate_workflows_s5.py",
        "tests/workflows",
        "tests/hermes_runtime/test_skill_composition.py",
        "tests/hermes_runtime/test_skills.py",
        "scripts/run_workflow_demo.py",
        "scripts/preview_remote_frontend.py",
    ]
    phases = [
        ("ruff", [python, "-B", "-m", "ruff", "check", *files], 60),
        ("format", [python, "-B", "-m", "ruff", "format", "--check", *files], 60),
        (
            "native-import",
            [
                hermes,
                "-B",
                "-c",
                "import sys;sys.path.insert(0,'src');from hermes_runtime.workflow_bridge import WorkflowBridge;from hermes_runtime.job_context import load_approved_job;assert 'numpy' not in sys.modules;assert 'application.skill_job_factory' not in sys.modules;print('Native workflow tools import without numerical runtime')",
            ],
            30,
        ),
        (
            "full",
            [
                python,
                "-B",
                "-m",
                "pytest",
                "tests",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp=" + str(home / "pytest"),
            ],
            600,
        ),
        ("pip-app", [python, "-B", "-m", "pip", "check"], 60),
        ("pip-hermes", [hermes, "-B", "-m", "pip", "check"], 60),
    ]
    report = {
        "status": "running",
        "pid": os.getpid(),
        "python": python,
        "started_at": time.time(),
        "phases": [],
    }
    target = ROOT / "docs/validation" / (prefix + "-driver.json")
    try:
        for name, command, timeout in phases:
            log_path = ROOT / "docs/validation" / (prefix + "-" + name + ".log")
            start = time.monotonic()
            with log_path.open("x", encoding="utf-8") as stream:
                log_path.chmod(0o600)
                child = subprocess.Popen(
                    command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT
                )
                try:
                    code = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                    code = 124
            report["phases"].append(
                {
                    "name": name,
                    "returncode": code,
                    "seconds": round(time.monotonic() - start, 2),
                    "log": str(log_path.relative_to(ROOT)),
                }
            )
            target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            if code:
                report["status"] = "failed"
                return 1
        report["status"] = "passed"
        return 0
    finally:
        report["finished_at"] = time.time()
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(target), "status": report["status"]}), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
