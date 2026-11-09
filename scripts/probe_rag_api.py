"""One bounded real API/skill/RAG integration; unrelated MCP operations are synthetic."""

from __future__ import annotations

import argparse
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
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-06b/api-integration"
PYTHON = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    global OUT

    if sys.executable != PYTHON:
        raise ValueError("use_existing_scat_hermes")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", choices=["v1", "v2"], default="v1")
    args = parser.parse_args()
    if args.revision != "v1":
        OUT = OUT.with_name(OUT.name + "-" + args.revision)
    import httpx

    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    marker = OUT / "started.json"
    if marker.exists():
        raise ValueError("integration_preserved_no_replay")
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", 18131)) == 0:
            raise ValueError("integration_port_already_owned")
    home = OUT / "home"
    command = [
        PYTHON,
        "-B",
        "scripts/run_hermes_api.py",
        "--profile",
        "scat-main",
        "--home",
        str(home.relative_to(ROOT)),
        "--port",
        "18131",
        "--with-mcp",
        "--with-skills",
        "--synthetic-mcp",
        "--skills-policy",
        "config/rag-readonly-policy.json",
        "--readonly-budget",
        json.dumps({"max_llm_tokens": 700000, "max_turns": 28, "max_tool_calls": 64}),
        "--rag-config",
        "services/rag/real-agent-06b-service.json",
    ]
    save(
        marker,
        {
            "command": command,
            "scope": "isolated-opt-in-real-rag-synthetic-other-mcp",
            "sources": {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in (
                    "scripts/probe_rag_api.py",
                    "scripts/run_hermes_api.py",
                    "src/hermes_runtime/rag_operation.py",
                    "src/hermes_runtime/real_rag.py",
                    "src/hermes_runtime/real_rag_native.py",
                    "scripts/run_real_agent_rag.py",
                    "services/rag/real-agent-06b-service.json",
                    "config/rag-readonly-policy.json",
                )
            },
        },
    )
    report = {"status": "running", "real_corpus_rag": True, "other_mcp": "synthetic", "checks": {}}
    process = None
    try:
        with (OUT / "api.log").open("w") as log:
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        sys.path.insert(0, str(ROOT / "src"))
        from hermes_runtime.rag_operation import process_identity

        birth = process_identity(process.pid)
        save(OUT / "driver.json", {"pid": process.pid, "start_id": birth})
        with httpx.Client(base_url="http://127.0.0.1:18131", trust_env=False, timeout=15) as client:
            for _ in range(90):
                if process.poll() is not None:
                    raise RuntimeError("api_start_failed")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("api_health_timeout")
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            scope = "rag-api-" + uuid4().hex
            headers = {
                "Authorization": "Bearer " + key,
                "X-Hermes-Session-Key": scope,
                "Idempotency-Key": scope + ":" + str(uuid4()),
            }
            report["checks"]["missing_auth_rejected"] = (
                client.post("/v1/runs", json={"input": "x"}).status_code == 401
            )
            payload = {
                "input": "请先加载aviation-knowledge技能，再调用search_scat_knowledge，mode必须为deep。问题：文档中FL（Flight Level，飞行高度层）是什么意思，按什么单位表示？只进行这一次深度知识查询，引用实际来源，不进行其他业务操作。"
            }
            response = client.post("/v1/runs", headers=headers, json=payload)
            response.raise_for_status()
            admitted = response.json()
            rid = admitted["run_id"]
            save(OUT / "admitted-private.json", {"scope": scope, "response": admitted})
            duplicate = client.post("/v1/runs", headers=headers, json=payload)
            report["checks"]["api_idempotent_same_run"] = (
                duplicate.status_code == 202
                and duplicate.json().get("run_id") == rid
                and duplicate.json().get("replayed") is True
            )
            other = dict(headers) | {"X-Hermes-Session-Key": "other-" + uuid4().hex}
            report["checks"]["cross_scope_rejected"] = client.get(
                f"/v1/runs/{rid}", headers=other
            ).status_code in {403, 404}
            started = time.monotonic()
            terminal = {}
            while time.monotonic() - started < 640:
                response = client.get(f"/v1/runs/{rid}", headers=headers)
                response.raise_for_status()
                terminal = response.json()
                save(OUT / "run-private.json", terminal)
                if terminal.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(1)
            else:
                client.post(f"/v1/runs/{rid}/stop", headers=headers, json={})
                raise TimeoutError("integration_run_deadline")
            report["terminal_status"] = terminal.get("status")
            report["elapsed_seconds"] = time.monotonic() - started
            report["checks"]["api_run_completed"] = terminal.get("status") == "completed"
            state = (
                ROOT / "var/rag-operations" / hashlib.sha256(str(home).encode()).hexdigest()[:20]
            )
            with sqlite3.connect(state / "invocations.sqlite") as db:
                db.row_factory = sqlite3.Row
                rows = [
                    dict(x)
                    for x in db.execute(
                        "SELECT * FROM rag_invocations WHERE scope=? AND run_id=?", (scope, rid)
                    )
                ]
            report["operation_statuses"] = [x["status"] for x in rows]
            report["checks"]["one_real_deep_operation"] = (
                len(rows) == 1 and rows[0]["status"] == "succeeded"
            )
            if rows:
                nested = json.loads((Path(rows[0]["output"]) / "result.json").read_text())
                report["nested"] = {
                    k: nested.get(k)
                    for k in ("status", "mode", "elapsed_seconds", "checks", "budget")
                }
                report["checks"]["four_native_sessions"] = len(nested.get("agents", {})) == 4
                report["checks"]["deep_result_passed"] = (
                    nested.get("status") == "passed" and nested.get("mode") == "deep"
                )
            with sqlite3.connect(home / "skill-loads.sqlite") as db:
                db.row_factory = sqlite3.Row
                parent = dict(
                    db.execute(
                        "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?", (scope, rid)
                    ).fetchone()
                )
                ops = [
                    dict(x)
                    for x in db.execute(
                        "SELECT operation,skill_id,outcome,load_id FROM skill_operations WHERE scope=? AND run_id=?",
                        (scope, rid),
                    )
                ]
            report["parent_budget"] = {
                k: v for k, v in parent.items() if k not in {"scope", "run_id"}
            }
            report["skill_operations"] = ops
            report["checks"]["actual_loaded_knowledge_skill"] = (
                len(ops) == 1
                and ops[0]["skill_id"] == "aviation-knowledge"
                and ops[0]["outcome"] == "returned"
            )
            report["checks"]["shared_parent_budget"] = (
                bool(rows)
                and parent["llm_calls"] > nested["budget"]["llm_calls"]
                and parent["llm_calls"] <= 28
                and parent["charged_tokens"] <= 700000
            )
            report["checks"]["budget_not_exhausted"] = parent["exhausted"] is None
            report["status"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:  # noqa: BLE001 -- only sanitized error type is exported
        report.update(status="failed", error_type=type(exc).__name__)
    finally:
        if process is not None and process.poll() is None:
            from hermes_runtime.rag_operation import process_identity

            if process_identity(process.pid) == birth:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if process_identity(process.pid) == birth:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        report["api_stopped"] = process is None or process.poll() is not None
        save(OUT / "summary.json", report)
        public = ROOT / "docs/validation/20261026-rag-06b-api.json"
        save(public, report)
        print(json.dumps({"status": report["status"], "checks": report["checks"]}))


if __name__ == "__main__":
    main()
