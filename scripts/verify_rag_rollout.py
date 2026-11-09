"""Exercise only the owned RAG deployment rollback and restore without model calls."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    import httpx

    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "src"))
    from evaluate_live_rag40 import api_usage, validate_receipts
    from manage_rag_production import OPTIONS, STATE, UNITS, health, status

    from rag_retrieval.runtime import load_chunk_records

    os.umask(0o077)
    output = ROOT / "docs/validation/20261026-rag-live-rollback.json"
    if output.exists():
        raise ValueError("rollback_report_preserved")
    checks = {}
    before = api_usage()
    original = json.loads((STATE / "previous-rag-options.json").read_text())

    def old_identity():
        value = health(8091)
        if not value or value.get("status") != "ready":
            return None
        return {k: v for k, v in value.items() if k != "uptime_seconds"}

    old_health = old_identity()
    command = [sys.executable, "-B", "scripts/manage_rag_production.py"]
    try:
        subprocess.run(command + ["rollback"], cwd=ROOT, check=True, timeout=150)
        stopped = status()
        lines = (ROOT / ".env").read_text().splitlines()
        restored = {
            k: [line for line in lines if re.match(r"^(?:export\s+)?" + k + r"\s*=", line)]
            for k in OPTIONS
        }
        checks["owned_units_stopped_and_disabled"] = all(
            stopped["units"][name]["active"] == "inactive"
            and stopped["units"][name]["enabled"] in {"disabled", "linked"}
            for name in UNITS
        )
        checks["previous_rag_options_restored"] = restored == original
        checks["old_8091_survived_rollback"] = bool(old_health) and old_identity() == old_health
    finally:
        subprocess.run(command + ["start"], cwd=ROOT, check=True, timeout=210)
    running = status()
    checks["owned_units_active_and_enabled"] = all(
        running["units"][name] == {"active": "active", "enabled": "enabled"} for name in UNITS
    )
    checks["pinned_rag_ready"] = running["rag_ready"]
    checks["old_8091_unchanged"] = bool(old_health) and old_identity() == old_health
    test = ROOT / "var/validation/20261026-rag-live/test40"
    admitted = json.loads((test / "LIVE-20261026-001-fast/admitted.json").read_text())
    key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
    headers = {"Authorization": "Bearer " + key, "X-Hermes-Session-Key": admitted["scope"]}
    with httpx.Client(base_url="http://127.0.0.1:18081", trust_env=False, timeout=10) as client:
        response = client.get("/v1/runs/" + admitted["run_id"], headers=headers)
        checks["completed_run_survives_full_rollback_restart"] = (
            response.status_code == 200 and response.json()["status"] == "completed"
        )
        headers["X-Hermes-Session-Key"] = "scat:" + "0" * 64
        checks["restored_run_remains_scope_isolated"] = (
            client.get("/v1/runs/" + admitted["run_id"], headers=headers).status_code == 404
        )
    config = json.loads((ROOT / "services/rag/qwen3-production-v1.json").read_text())
    receipts = json.loads(
        (test / "repair-v2/LIVE-20261026-004-fast-retry/knowledge-receipts.json").read_text()
    )
    _, records = load_chunk_records(Path(config["corpus"]))
    retry_scope = (
        "scat:" + __import__("hashlib").sha256(b"live40-known-terminal-retry:004").hexdigest()
    )
    sources, ids = validate_receipts(receipts, records, config, retry_scope)
    checks["retry_actual_receipts_independently_validated"] = bool(sources and ids)
    checks["no_model_requests_or_tokens_replayed"] = api_usage() == before
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "final_units": running["units"],
        "corpus_sha256": config["corpus_sha256"],
        "external_model_requests": 0,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report))
    if report["status"] != "passed":
        raise ValueError("rollout_verification_failed")


if __name__ == "__main__":
    main()
