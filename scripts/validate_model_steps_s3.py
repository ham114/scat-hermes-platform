"""Finite S3 server acceptance driver. Logs and temporary files stay inside this project."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main(include_real_llm):
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    identity = uuid4().hex[:10]
    prefix = "20261023-S3-" + identity
    target = ROOT / "docs/validation" / (prefix + "-driver.json")
    home = ROOT / ".tmp/s3" / ("validation-" + identity)
    home.mkdir(parents=True)
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(home),
        "TMP": str(home),
        "TEMP": str(home),
    }
    python = str(Path(sys.executable).resolve())
    hermes = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"
    files = [
        "src/scat_model_runtime/steps.py",
        "src/scat_model_runtime/step_codec.py",
        "src/scat_model_runtime/cpu_fixture.py",
        "src/application/model_step_store.py",
        "src/application/model_steps.py",
        "src/application/model_records.py",
        "src/application/model_tasks.py",
        "src/hermes_runtime/model_step_bridge.py",
        "scripts/run_model_step_tool.py",
        "scripts/probe_model_steps_s3.py",
        "scripts/validate_model_steps_s3.py",
        "scripts/run_hermes_api.py",
        "tests/analysis_models/test_model_steps.py",
        "tests/hermes_runtime/test_skills.py",
        "tests/hermes_runtime/test_skill_composition.py",
        "tests/hermes_runtime/test_model_step_bridge.py",
    ]
    phases = [
        ("ruff", [python, "-B", "-m", "ruff", "check", *files], 60),
        ("format", [python, "-B", "-m", "ruff", "format", "--check", *files], 60),
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
            240,
        ),
        ("pip-app", [python, "-B", "-m", "pip", "check"], 60),
        ("pip-hermes", [hermes, "-B", "-m", "pip", "check"], 60),
    ]
    if include_real_llm:
        phases.append(("hermes-cpu", [python, "-B", "scripts/probe_model_steps_s3.py"], 360))
    report = {
        "status": "running",
        "pid": os.getpid(),
        "started_at": time.time(),
        "python": python,
        "include_real_llm": include_real_llm,
        "phases": [],
    }
    with target.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    try:
        for name, command, timeout in phases:
            log_path = ROOT / "docs/validation" / (prefix + "-" + name + ".log")
            started = time.monotonic()
            with log_path.open("x", encoding="utf-8") as stream:
                log_path.chmod(0o600)
                try:
                    result = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        check=False,
                    )
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    code = 124
            report["phases"].append(
                {
                    "name": name,
                    "returncode": code,
                    "seconds": round(time.monotonic() - started, 2),
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-real-llm", action="store_true", help="Paid DeepSeek, synthetic CPU only"
    )
    raise SystemExit(main(parser.parse_args().include_real_llm))
