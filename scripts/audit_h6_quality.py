"""Activate the verified API revision when idle, then audit H6 backend quality."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261027-h6-quality"
HOME = ROOT / "var/hermes/rag-production"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]
    import httpx
    from audit_rag_06b import open_port, sha
    from dotenv import dotenv_values
    from evaluate_live_rag40 import api_usage
    from manage_rag_production import health, status
    from verify_real_agent_rag import verify_result

    from hermes_runtime.native_budget import verify_budget_source
    from hermes_runtime.native_profiles import verify_native
    from hermes_runtime.real_rag import load_corpus

    suites = list(ET.parse(OUT / "full-tests-final.xml").getroot().iter("testsuite"))
    tests = {
        k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")
    }
    if tests["failures"] or tests["errors"] or tests["tests"] - tests["skipped"] < 1289:
        raise ValueError("final_project_tests_not_passed")
    first = read(ROOT / "docs/validation/20261027-h6-quality-live.json")
    fixed = read(ROOT / "docs/validation/20261027-h6-quality-live-intent-v2.json")
    if fixed["status"] != "passed" or not all(
        all(c["checks"].values()) for c in first["cases"][:2]
    ):
        raise ValueError("actual_regression_not_passed")
    checks = {}
    old_health = health(18133)
    before = api_usage()
    with sqlite3.connect(HOME / "runs_idempotency.db") as db:
        statuses = [
            json.loads(row[0]) for row in db.execute("SELECT status_json FROM run_idempotency")
        ]
    if any(row.get("status") in {"queued", "pending", "running", "starting"} for row in statuses):
        raise ValueError("production_api_busy_do_not_interrupt")
    unit = Path("/home/h3c/.config/systemd/user/scat-hermes-api.service")
    if (
        not unit.is_symlink()
        or unit.resolve() != ROOT / "infra/systemd/user/scat-hermes-api.service"
    ):
        raise ValueError("unknown_api_unit")
    subprocess.run(
        ["systemctl", "--user", "restart", "scat-hermes-api.service"], check=True, timeout=120
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if health(18081):
            break
        time.sleep(0.5)
    else:
        raise TimeoutError("production_api_not_ready")
    admitted = read(
        ROOT / "var/validation/20261026-rag-live/test40/LIVE-20261026-001-fast/admitted.json"
    )
    headers = {
        "Authorization": "Bearer " + (ROOT / "var/secrets/hermes-api.key").read_text().strip(),
        "X-Hermes-Session-Key": admitted["scope"],
    }
    with httpx.Client(trust_env=False, timeout=15) as client:
        result = client.get("http://127.0.0.1:18081/v1/runs/" + admitted["run_id"], headers=headers)
        checks["historical_completed_run_readable"] = (
            result.status_code == 200 and result.json()["status"] == "completed"
        )
        headers["X-Hermes-Session-Key"] = "scat:" + "0" * 64
        checks["historical_run_scope_isolated"] = (
            client.get(
                "http://127.0.0.1:18081/v1/runs/" + admitted["run_id"], headers=headers
            ).status_code
            == 404
        )
    checks["activation_no_model_replay"] = before == api_usage()
    current = status()
    checks["formal_services_active_enabled"] = (
        current["rag_ready"]
        and bool(current["hermes_health"])
        and all(
            value == {"active": "active", "enabled": "enabled"}
            for value in current["units"].values()
        )
    )
    checks["rag_identity_unchanged"] = old_health == health(18133)
    runtime_config = read(HOME / "config.yaml")
    checks["new_question_policy_active"] = (
        "文档标题及source_id不能代替" in runtime_config["platform_hints"]["api_server"]["append"]
    )
    verify_native(ROOT)
    verify_budget_source(ROOT, ROOT / "vendor/hermes-agent")
    checks["native_and_budget_pins_intact"] = True
    baseline = read(OUT / "preflight.json")
    checks["conda_histories_unchanged"] = all(
        sha(Path("/home/h3c/anaconda3/envs") / name / "conda-meta/history") == value
        for name, value in baseline["history"].items()
    )
    old = read(ROOT / "services/rag/qwen3-06b-dev40-v1.json")
    checks["original_frozen_inputs_unchanged"] = all(
        sha(Path(item["path"])) == item["sha256"] for item in old["inputs"].values()
    )
    prior_sync = read(ROOT / "docs/validation/20261026-rag-live-synchronized-files.json")["sha256"]
    checks["new_ragas40_and_historical_scores_unchanged"] = all(
        sha(ROOT / name) == expected
        for name, expected in prior_sync.items()
        if name.startswith(
            (
                "evaluations/rag_live40_",
                "docs/ragas-review/20261026-live40/",
                "docs/validation/20261026-rag-live-test40",
            )
        )
    )
    config = read(ROOT / "services/rag/real-agent-production-v1.json")
    records = load_corpus(config)
    checks["production_corpus_pinned"] = len(records) == 149
    nested_proofs = []
    nested_folders = []
    for phase in ["live", "live-intent-v2"]:
        for receipt in read(OUT / phase / "filename-deep/receipts.json"):
            data = receipt.get("data", {})
            if "claims" not in data:
                continue
            request_id = receipt.get("request_id")
            folder = ROOT / "var/validation/rag-operations" / request_id
            nested_folders.append(folder)
            report = read(folder / "result.json")
            local = dict(config)
            if data.get("claims"):
                local["scope"] = data["claims"][0]["evidence"][0]["reference"]["scope"]
            nested_proofs.append({"phase": phase, **verify_result(report, records, local)})
    checks["real_deep_evidence_independently_verified"] = bool(nested_proofs) and all(
        x["execution_passed"] for x in nested_proofs
    )
    ports = {
        str(p): open_port(p) for p in [80, 443, 8080, 8501, 8091, 27017, 18081, 18133, 18135, 18136]
    }
    checks["old_six_ports_intact"] = all(ports[str(p)] for p in [80, 443, 8080, 8501, 8091, 27017])
    checks["isolated_ports_closed"] = not ports["18135"] and not ports["18136"]
    checks["same_total_model_budget"] = (
        fixed["all_phases_model_requests"] <= 80
        and fixed["all_phases_conservative_tokens"] <= 3000000
    )
    secrets = set()
    envs = [
        ROOT / ".env",
        HOME / ".env",
        *sorted((ROOT / "var/hermes/managed/profiles").glob("*/.env")),
    ]
    for path in envs:
        if path.is_file():
            for name, value in dotenv_values(path).items():
                if (
                    value
                    and len(value) >= 12
                    and any(term in name.upper() for term in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
                ):
                    secrets.add(value.encode())
    secrets.add((ROOT / "var/secrets/hermes-api.key").read_bytes().strip())
    scan = set()
    for folder in [
        OUT,
        *nested_folders,
        ROOT / "docs",
        ROOT / "src/application",
        ROOT / "src/workflow_runtime",
        ROOT / "src/schema",
        ROOT / "src/hermes_runtime",
    ]:
        scan.update(
            p
            for p in folder.rglob("*")
            if p.is_file() and p.suffix in {".json", ".jsonl", ".py", ".md", ".txt", ".xml", ".log"}
        )
    scan.update(
        [
            ROOT / "scripts/probe_h6_quality.py",
            ROOT / "scripts/audit_h6_quality.py",
            ROOT / "scripts/run_hermes_api.py",
        ]
    )
    exposures = [
        str(p.relative_to(ROOT))
        for p in sorted(scan)
        if any(secret in p.read_bytes() for secret in secrets)
    ]
    checks["actual_private_value_scan_clean"] = not exposures
    checks["task_scratch_removed"] = not (ROOT / ".tmp/h6-quality").exists()
    report = {
        "status": "passed" if all(checks.values()) else "incomplete",
        "checks": checks,
        "test_summary": tests,
        "real_regression": fixed,
        "deep_provenance": nested_proofs,
        "ports": ports,
        "credential_scan": {"files": len(scan), "exposure_files": exposures},
        "whole_h6_frontend_complete": False,
        "h7b_h8_complete": False,
        "human_rag_admission": False,
    }
    path = ROOT / "docs/validation/20261027-h6-quality-acceptance.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    if report["status"] != "passed":
        raise ValueError("h6_final_audit_incomplete")


if __name__ == "__main__":
    main()
