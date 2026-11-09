"""Verify the actual production skill path and bounded service crash recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-live/smoke"
HOME = ROOT / "var/hermes/rag-production"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    import httpx

    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "started.json").exists():
        raise ValueError("production_smoke_preserved_no_replay")
    (OUT / "started.json").write_text(json.dumps({"started_at": time.time(), "model_run_limit": 1}))
    key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
    scope = "scat:" + hashlib.sha256(b"20261026-rag-production-smoke").hexdigest()
    headers = {
        "Authorization": "Bearer " + key,
        "X-Hermes-Session-Key": scope,
        "Idempotency-Key": scope + ":" + str(uuid4()),
    }
    payload = {
        "input": "请加载知识技能，使用普通知识检索mode=fast核实：修订后的SCAT Table 1B七个包的航班小计与全部十三个包的总计分别是多少？引用实际source_id。不要进行深度委托或其他业务操作。"
    }
    checks = {}
    with httpx.Client(base_url="http://127.0.0.1:18081", trust_env=False, timeout=15) as client:
        response = client.post("/v1/runs", headers=headers, json=payload)
        response.raise_for_status()
        rid = response.json()["run_id"]
        (OUT / "request-private.json").write_text(
            json.dumps(
                {
                    "headers_without_auth": {
                        k: v for k, v in headers.items() if k != "Authorization"
                    },
                    "payload": payload,
                    "run_id": rid,
                }
            )
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = client.get(f"/v1/runs/{rid}", headers=headers)
            response.raise_for_status()
            result = response.json()
            if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.5)
        else:
            client.post(f"/v1/runs/{rid}/stop", headers=headers, json={})
            raise TimeoutError("production_smoke_deadline")
        (OUT / "run-private.json").write_text(json.dumps(result, ensure_ascii=False))
        checks["real_run_completed"] = result["status"] == "completed"
        with sqlite3.connect(ROOT / "var/platform/hermes-results.sqlite") as db:
            entries = db.execute(
                "SELECT payload FROM tool_results WHERE scope_key=? AND run_id=? AND operation='search_scat_knowledge'",
                (scope, rid),
            ).fetchall()
        payloads = [json.loads(row[0]) for row in entries]
        expected = json.loads((ROOT / "services/rag/qwen3-production-v1.json").read_text())
        checks["real_knowledge_receipt"] = bool(payloads)
        checks["actual_fast_tool_uses_06b_v4"] = bool(payloads) and all(
            p["data"].get("profile_id") == expected["profile_id"]
            and p["data"].get("corpus_sha256") == expected["corpus_sha256"]
            for p in payloads
        )
        with sqlite3.connect(HOME / "skill-loads.sqlite") as db:
            db.row_factory = sqlite3.Row
            budget = dict(
                db.execute(
                    "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?", (scope, rid)
                ).fetchone()
            )
            operations = [
                dict(x)
                for x in db.execute(
                    "SELECT skill_id,operation,outcome FROM skill_operations WHERE scope=? AND run_id=?",
                    (scope, rid),
                )
            ]
        checks["knowledge_body_was_loaded"] = any(
            x["skill_id"] == "aviation-knowledge"
            and x["operation"] == "mcp__scat__search_scat_knowledge"
            and x["outcome"] == "returned"
            for x in operations
        )
        duplicate = client.post("/v1/runs", headers=headers, json=payload)
        checks["request_idempotent"] = (
            duplicate.status_code == 202
            and duplicate.json()["run_id"] == rid
            and duplicate.json()["replayed"] is True
        )
        other = headers | {"X-Hermes-Session-Key": "scat:" + "0" * 64}
        checks["cross_scope_status_denied"] = (
            client.get(f"/v1/runs/{rid}", headers=other).status_code == 404
        )
        mainpid = subprocess.run(
            ["systemctl", "--user", "show", "scat-hermes-rag.service", "-p", "MainPID", "--value"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        proc = Path("/proc") / mainpid
        if proc.joinpath(
            "cwd"
        ).resolve() != ROOT or b"scripts/serve_rag_production.py" not in proc.joinpath(
            "cmdline"
        ).read_bytes().split(b"\0"):
            raise ValueError("refuse_crash_test_on_unknown_process")
        subprocess.run(
            [
                "systemctl",
                "--user",
                "kill",
                "--kill-who=main",
                "--signal=KILL",
                "scat-hermes-rag.service",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        deadline = time.monotonic() + 90
        recovered = None
        while time.monotonic() < deadline:
            try:
                health = httpx.get("http://127.0.0.1:18133/health", trust_env=False, timeout=3)
                if (
                    health.status_code == 200
                    and health.json().get("corpus_sha256") == expected["corpus_sha256"]
                ):
                    recovered = health.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        checks["retriever_restarts_and_same_corpus_recovers"] = recovered is not None
        restarted_pid = subprocess.run(
            ["systemctl", "--user", "show", "scat-hermes-rag.service", "-p", "MainPID", "--value"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        checks["new_owned_retriever_process"] = restarted_pid not in {mainpid, "0", ""}
        subprocess.run(
            ["systemctl", "--user", "restart", "scat-hermes-api.service"],
            capture_output=True,
            check=True,
            timeout=120,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                response = client.get(f"/v1/runs/{rid}", headers=headers)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        checks["api_run_survives_restart"] = (
            response.status_code == 200 and response.json()["status"] == "completed"
        )
        with sqlite3.connect(HOME / "skill-loads.sqlite") as db:
            after = db.execute(
                "SELECT llm_calls,charged_tokens FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (scope, rid),
            ).fetchone()
        checks["recovery_did_not_replay_models"] = after == (
            budget["llm_calls"],
            budget["charged_tokens"],
        )
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "model_requests": budget["llm_calls"],
        "conservative_tokens": budget["charged_tokens"],
        "skill_operations": operations,
        "real_backend": True,
        "synthetic_mcp": False,
    }
    (OUT / "summary.json").write_text(json.dumps(report, indent=2))
    (ROOT / "docs/validation/20261026-rag-live-smoke.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
