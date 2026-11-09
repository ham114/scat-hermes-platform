"""Bounded real API regression; no judge, answer retries, or historical score rewrite."""

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
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261027-h6-quality/live"
HOME = ROOT / ".tmp/h6-quality/api-home"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(*, repair_intent=False):
    global OUT, HOME
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    import httpx

    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
    from evaluate_live_rag40 import validate_receipts

    from rag_retrieval.runtime import load_chunk_records

    os.umask(0o077)
    prior_calls = prior_tokens = 0
    report_path = ROOT / "docs/validation/20261027-h6-quality-live.json"
    if repair_intent:
        previous = json.loads(report_path.read_text())
        if previous["status"] != "failed" or previous["cases"][-1]["checks"]["direct_answer"]:
            raise ValueError("expected_recorded_deep_intent_failure")
        prior_calls = previous["model_requests"]
        prior_tokens = previous["conservative_tokens"]
        if prior_calls + 26 > 80 or prior_tokens + 1000000 > 3000000:
            raise ValueError("same_campaign_budget_insufficient")
        OUT = OUT.parent / "live-intent-v2"
        HOME = HOME.parent / "api-home-intent-v2"
        report_path = ROOT / "docs/validation/20261027-h6-quality-live-intent-v2.json"
    if (OUT / "started.json").exists():
        raise ValueError("h6_paid_regression_preserved_do_not_replay")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 18135)) == 0:
            raise ValueError("refuse_existing_listener")
    cases = [
        {
            "id": "filename-fast",
            "mode": "fast",
            "question": "SCAT 数据集事实看哪个文件？",
            "required": ["SCAT_DATASET.md"],
        },
        {
            "id": "table-scope-fast",
            "mode": "fast",
            "question": "SCAT 修订资料中 Table 1B 七个包的小计及全部十三个包的航班总计分别是多少？",
            "required": ["94992", "167547"],
        },
        {
            "id": "filename-deep",
            "mode": "deep",
            "question": "SCAT 数据集事实看哪个文件？",
            "required": ["SCAT_DATASET.md"],
        },
    ]
    if repair_intent:
        cases = [case for case in cases if case["id"] == "filename-deep"]
    save(
        OUT / "started.json",
        {
            "cases": cases,
            "cap_calls": 80,
            "cap_conservative": 3000000,
            "per_run": {
                "max_turns": 26,
                "max_llm_tokens": 1000000,
                "max_tool_calls": 60,
                "wall_seconds": 360,
            },
            "answer_reference_not_sent": True,
            "no_sdk_retries": True,
            "prior_calls_not_reset": prior_calls,
            "prior_conservative_not_reset": prior_tokens,
        },
    )
    limits = {"max_turns": 26, "max_llm_tokens": 1000000, "max_tool_calls": 60, "wall_seconds": 360}
    command = [
        "/home/h3c/anaconda3/envs/scat-hermes/bin/python",
        "-B",
        "scripts/run_hermes_api.py",
        "--profile",
        "scat-main",
        "--home",
        str(HOME.relative_to(ROOT)),
        "--port",
        "18135",
        "--with-mcp",
        "--with-skills",
        "--skills-policy",
        "config/rag-readonly-policy.json",
        "--rag-config",
        "services/rag/real-agent-production-v1.json",
        "--readonly-budget",
        json.dumps(limits),
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=(OUT / "api.log").open("w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
    )
    save(OUT / "api-process.json", {"pid": process.pid, "command": command})
    results = []
    config = json.loads((ROOT / "services/rag/qwen3-production-v1.json").read_text())
    _, records = load_chunk_records(Path(config["corpus"]))
    try:
        with httpx.Client(base_url="http://127.0.0.1:18135", trust_env=False, timeout=20) as client:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("isolated_api_exited")
                time.sleep(0.5)
            else:
                raise TimeoutError("isolated_api_not_ready")
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            for case in cases:
                folder = OUT / case["id"]
                scope = "scat:" + hashlib.sha256(("h6-quality:" + case["id"]).encode()).hexdigest()
                headers = {
                    "Authorization": "Bearer " + key,
                    "X-Hermes-Session-Key": scope,
                    "Idempotency-Key": str(uuid4()),
                }
                prefix = (
                    "请先加载适用知识技能，仅按知识文档回答，引用实际source_id；使用mode="
                    + case["mode"]
                    + "。"
                )
                save(
                    folder / "request.json",
                    {
                        "scope": scope,
                        "input": prefix + case["question"],
                        "idempotency_key": headers["Idempotency-Key"],
                    },
                )
                started = time.monotonic()
                response = client.post(
                    "/v1/runs", headers=headers, json={"input": prefix + case["question"]}
                )
                response.raise_for_status()
                rid = response.json()["run_id"]
                save(folder / "admitted.json", {"run_id": rid, "scope": scope})
                deadline = time.monotonic() + 390
                while time.monotonic() < deadline:
                    response = client.get("/v1/runs/" + rid, headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                        break
                    time.sleep(0.5)
                else:
                    client.post("/v1/runs/" + rid + "/stop", headers=headers, json={})
                    raise TimeoutError("unknown_run_requires_reconciliation_no_replay")
                save(folder / "result.json", result)
                with sqlite3.connect(ROOT / "var/platform/hermes-results.sqlite") as db:
                    receipts = [
                        json.loads(row[0])
                        for row in db.execute(
                            "SELECT payload FROM tool_results WHERE scope_key=? AND run_id=? AND operation='search_scat_knowledge'",
                            (scope, rid),
                        )
                    ]
                save(folder / "receipts.json", receipts)
                sources, _ = validate_receipts(receipts, records, config, scope)
                answer = result.get("output", "")
                compact = answer.replace(",", "").replace("，", "")
                with sqlite3.connect(HOME / "skill-loads.sqlite") as db:
                    db.row_factory = sqlite3.Row
                    budget = dict(
                        db.execute(
                            "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                            (scope, rid),
                        ).fetchone()
                    )
                    operations = [
                        dict(x)
                        for x in db.execute(
                            "SELECT operation,skill_id,outcome FROM skill_operations WHERE scope=? AND run_id=?",
                            (scope, rid),
                        )
                    ]
                save(folder / "budget.json", budget)
                checks = {
                    "completed": result["status"] == "completed",
                    "actual_evidence": bool(receipts),
                    "direct_answer": all(word in compact for word in case["required"]),
                    "actual_source_cited": any(source in answer for source in sources),
                    "skill_body_loaded": any(
                        x["skill_id"] == "aviation-knowledge" and x["outcome"] == "returned"
                        for x in operations
                    ),
                    "budget_not_exhausted": budget["exhausted"] is None,
                }
                if case["mode"] == "deep":
                    checks["actual_deep_receipt"] = any(
                        "claims" in x.get("data", {}) for x in receipts
                    )
                results.append(
                    {
                        "id": case["id"],
                        "checks": checks,
                        "seconds": time.monotonic() - started,
                        "model_calls": budget["llm_calls"],
                        "conservative_tokens": budget["charged_tokens"],
                    }
                )
                save(OUT / "progress.json", results)
                if (
                    prior_calls + sum(x["model_calls"] for x in results) > 80
                    or prior_tokens + sum(x["conservative_tokens"] for x in results) > 3000000
                ):
                    raise ValueError("campaign_budget_exceeded")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        if (HOME / "skill-loads.sqlite").exists():
            import shutil

            shutil.copyfile(HOME / "skill-loads.sqlite", OUT / "budget.sqlite")
    report = {
        "status": "passed"
        if len(results) == len(cases) and all(all(x["checks"].values()) for x in results)
        else "failed",
        "cases": results,
        "model_requests": sum(x["model_calls"] for x in results),
        "conservative_tokens": sum(x["conservative_tokens"] for x in results),
        "historical_scores_unchanged": True,
        "independent_admission": False,
        "all_phases_model_requests": prior_calls + sum(x["model_calls"] for x in results),
        "all_phases_conservative_tokens": prior_tokens
        + sum(x["conservative_tokens"] for x in results),
    }
    save(report_path, report)
    print(json.dumps(report))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair-intent", action="store_true")
    main(repair_intent=parser.parse_args().repair_intent)
