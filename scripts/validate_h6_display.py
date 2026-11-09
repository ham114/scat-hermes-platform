"""Check shared H6 synthetic protocol fixtures and existing artifact/RAG contracts in Conda."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"


def main():
    from schema.analysis import AnalysisResult

    if sys.executable != APP or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise ValueError("existing_server_scat_agent_required")
    identity = uuid4().hex[:10]
    work = ROOT / ".tmp/h6a" / identity
    work.mkdir(parents=True)
    fixture = ROOT / "frontend/src/test-fixtures/analysis.json"
    raw = json.loads(fixture.read_text())
    result = AnalysisResult.model_validate(raw["analysis_result"])
    canonical = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == raw["payload_sha256"]
    assert {block.type for block in result.blocks} == {
        "metrics",
        "table",
        "timeseries",
        "trajectory_2d",
        "classification",
        "prediction",
        "action_link",
    }
    assert result.context["synthetic"] is True
    report = {
        "status": "running",
        "shared_fixture_schema_passed": True,
        "block_types": sorted(block.type for block in result.blocks),
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "payload_sha256": raw["payload_sha256"],
        "synthetic_only": True,
        "llm_calls": 0,
        "retrieval_model_benchmarked": False,
        "installs": 0,
        "new_environments": 0,
        "production_gpu": False,
    }
    log_path = ROOT / "var/validation" / ("20261025-H6A-" + identity + "-contracts.log")
    xml_path = ROOT / "var/validation" / ("20261025-H6A-" + identity + "-contracts.xml")
    env = os.environ | {
        "TMPDIR": str(work),
        "TMP": str(work),
        "TEMP": str(work),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        with log_path.open("x") as log:
            log_path.chmod(0o600)
            completed = subprocess.run(
                [
                    APP,
                    "-B",
                    "-m",
                    "pytest",
                    "tests/hermes_runtime/test_model_tasks.py",
                    "tests/agents/test_rag_tools.py",
                    "tests/rag_retrieval/test_runtime.py",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp=" + str(work / "pytest"),
                    "--junitxml=" + str(xml_path),
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                timeout=180,
                check=False,
            )
        suites = ET.parse(xml_path).getroot().findall("testsuite")
        report["pytest"] = {
            key: sum(int(s.get(key, 0)) for s in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        report["status"] = "passed" if completed.returncode == 0 else "failed"
    finally:
        assert work.parent.resolve() == (ROOT / ".tmp/h6a").resolve() and not work.is_symlink()
        for path in work.rglob("*"):
            if path.is_symlink():
                path.unlink()
        shutil.rmtree(work)
        report["temporary_removed"] = True
        path = ROOT / "docs/validation/20261025-H6A-backend.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
