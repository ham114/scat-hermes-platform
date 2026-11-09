"""Restart the isolated API and reopen actual RAG receipts without model replay."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-06b"
PYTHON = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if sys.executable != PYTHON:
        raise ValueError("use_existing_scat_hermes")
    import httpx

    os.umask(0o077)
    sys.path.insert(0, str(ROOT / "src"))
    from hermes_runtime.rag_operation import RagOperation, process_identity
    from hermes_runtime.skill_budget import RunBudget
    from hermes_runtime.skill_policy import Budget

    api = OUT / "api-integration-v2"
    admitted = read(api / "admitted-private.json")
    scope, rid = admitted["scope"], admitted["response"]["run_id"]
    home = api / "home"
    with sqlite3.connect(home / "skill-loads.sqlite") as db:
        db.row_factory = sqlite3.Row
        parent = dict(
            db.execute(
                "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?", (scope, rid)
            ).fetchone()
        )
    budget = RunBudget(
        home / "skill-loads.sqlite",
        scope,
        rid,
        Budget.model_validate_json(parent["limits_json"]),
        parent["policy_sha256"],
    )
    bridge = SimpleNamespace(
        _sessions={"owner": (scope, rid), "other": ("other", "other")}, _budgets={"owner": budget}
    )
    other = RunBudget(OUT / "recovery-other.sqlite", "other", "other", Budget(), "recovery-fixture")
    bridge._budgets["other"] = other
    bridge.session_bridge = lambda session: bridge
    state = ROOT / "var/rag-operations" / hashlib.sha256(str(home).encode()).hexdigest()[:20]
    operation = RagOperation(ROOT, state, bridge, ROOT / "services/rag/real-agent-06b-service.json")
    with sqlite3.connect(operation.path) as db:
        identity = db.execute(
            "SELECT id FROM rag_invocations WHERE scope=? AND run_id=?", (scope, rid)
        ).fetchone()[0]
    first = operation.status(identity, "owner")
    checks = {
        "cached_actual_receipt_after_reopen": first.get("status") == "ok"
        and first.get("reused") is True,
        "cross_scope_status_denied": operation.status(identity, "other")["status"] == "not_found",
        "cross_scope_cancel_denied": operation.cancel(identity, "other")["status"] == "not_found",
        "terminal_success_not_cancelled": operation.cancel(identity, "owner")["status"] == "ok",
    }
    fixture = RagOperation(ROOT, OUT / "recovery-cli-fixture", bridge, operation.config_path)
    with sqlite3.connect(fixture.path) as db:
        db.execute(
            "INSERT OR IGNORE INTO rag_invocations(id,scope,run_id,request_hash,status,output,started) VALUES(?,?,?,?,?,?,?)",
            (
                "rag-recovery-fixture",
                "fixture",
                "fixture",
                "fixture",
                "running",
                str(OUT / "never-executed-fixture"),
                time.time(),
            ),
        )
    cli_results = []
    for action in ("status", "reconcile", "cancel"):
        completed = subprocess.run(
            [
                PYTHON,
                "-B",
                "scripts/manage_rag_operations.py",
                "--state",
                str(fixture.path),
                "--id",
                "rag-recovery-fixture",
                "--action",
                action,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        cli_results.append(json.loads(completed.stdout))
    checks["orphan_reconciled_unknown_without_replay"] = (
        cli_results[1]["status"] == "unknown" and not cli_results[1]["automatic_replay"]
    )
    checks["unknown_cancel_does_not_restart"] = cli_results[2]["status"] == "unknown"
    command = read(api / "started.json")["command"]
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", 18131)) == 0:
            raise ValueError("recovery_port_already_in_use")
    marker = OUT / "recovery-started.json"
    if marker.exists():
        raise ValueError("recovery_probe_preserved")
    marker.write_text(json.dumps({"model_calls_allowed": 0}))
    process = None
    try:
        with (OUT / "recovery-api.log").open("w") as log:
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        birth = process_identity(process.pid)
        (OUT / "recovery-api-driver.json").write_text(
            json.dumps({"pid": process.pid, "start_id": birth})
        )
        with httpx.Client(base_url="http://127.0.0.1:18131", trust_env=False, timeout=10) as client:
            for _ in range(90):
                if process.poll() is not None:
                    raise RuntimeError("recovery_api_start_failed")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("recovery_health_deadline")
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            headers = {"Authorization": "Bearer " + key, "X-Hermes-Session-Key": scope}
            response = client.get(f"/v1/runs/{rid}", headers=headers)
            checks["api_terminal_state_survives_restart"] = (
                response.status_code == 200 and response.json().get("status") == "completed"
            )
            other_headers = headers | {"X-Hermes-Session-Key": "other"}
            checks["api_restart_cross_scope_denied"] = (
                client.get(f"/v1/runs/{rid}", headers=other_headers).status_code == 404
            )
        after = budget.snapshot()
        checks["no_model_or_tool_replay"] = all(
            parent[k] == after[k] for k in ("llm_calls", "charged_tokens", "tool_calls")
        )
        checks["receipt_unchanged"] = (
            operation.status(identity, "owner")["data"]["answer_sha256"]
            == first["data"]["answer_sha256"]
        )
    finally:
        if (
            process is not None
            and process.poll() is None
            and process_identity(process.pid) == birth
        ):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if process_identity(process.pid) == birth:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    result = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "model_requests": 0,
        "orphan_case": "synthetic-control-state-only",
        "actual_receipt_reused": True,
        "api_stopped": process.poll() is not None,
    }
    (ROOT / "docs/validation/20261026-rag-06b-recovery.json").write_text(
        json.dumps(result, indent=2)
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
