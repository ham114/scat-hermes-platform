"""Server-only H7-A checks; private logs, fixed metadata, existing Conda only."""

import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
NATIVE = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def main():
    if sys.executable != APP:
        raise RuntimeError("existing_scat_agent_required")
    identity = uuid4().hex[:10]
    work = ROOT / ".tmp/h7a" / ("checks-" + identity)
    work.mkdir(parents=True)
    env = os.environ | {
        "TMPDIR": str(work),
        "TMP": str(work),
        "TEMP": str(work),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUFF_CACHE_DIR": str(work / "ruff"),
    }
    paths = [
        "src/backend_ops",
        "scripts/manage_backend.py",
        "scripts/manage_backend_environment.py",
        "scripts/probe_backend_operations.py",
        "scripts/validate_backend_operations.py",
        "tests/hermes_runtime/test_backend_operations.py",
    ]
    xml = ROOT / "var/validation" / ("20261024-H7A-" + identity + "-pytest.xml")
    checks = [
        ("lint", [APP, "-B", "-m", "ruff", "check", *paths], 60),
        ("format", [APP, "-B", "-m", "ruff", "format", "--check", *paths], 60),
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
                "--junitxml=" + str(xml),
            ],
            900,
        ),
        ("environment-verify", [APP, "-B", "scripts/manage_backend_environment.py", "verify"], 60),
        ("app-pip-check", [APP, "-B", "-m", "pip", "check"], 60),
        ("native-pip-check", [NATIVE, "-B", "-m", "pip", "check"], 60),
    ]
    report = {"status": "running", "checks": [], "frontend_modified": False, "installs": 0}
    try:
        for name, command, timeout in checks:
            start = time.monotonic()
            log_path = ROOT / "var/validation" / ("20261024-H7A-" + identity + "-" + name + ".log")
            with log_path.open("x") as log:
                log_path.chmod(0o600)
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
                "seconds": round(time.monotonic() - start, 2),
                "log": log_path.relative_to(ROOT).as_posix(),
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
        if xml.exists():
            suites = ET.parse(xml).getroot().findall("testsuite")
            report["pytest"] = {
                k: sum(int(s.get(k, 0)) for s in suites)
                for k in ("tests", "failures", "errors", "skipped")
            }
    finally:
        shutil.rmtree(work)
        report["temporary_removed"] = not work.exists()
        path = ROOT / "docs/validation" / ("20261024-H7A-" + identity + "-checks.json")
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps({"report": path.relative_to(ROOT).as_posix(), "status": report["status"]}),
            flush=True,
        )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
