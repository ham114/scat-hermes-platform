"""Bounded DeepSeek + pinned Hermes + synthetic readonly MCP skill acceptance probe."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application.context import ExecutionContext
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.registry import RunRegistry
from hermes_runtime.results import ToolResultStore


async def probe(report):
    identity = uuid4().hex[:12]
    home = ROOT / ".tmp/s2dev" / ("runtime-" + identity)
    home.mkdir(parents=True)
    log_path = ROOT / "var/validation" / ("hermes-s2-" + identity + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    port = 18089
    origin = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
        try:
            await health.get(origin + "/health")
        except httpx.TransportError:
            pass
        else:
            raise RuntimeError("probe_port_in_use")
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(home),
        "TMP": str(home),
        "TMPDIR": str(home),
        "HERMES_RESULT_STATE_FILE": str(home / "results.sqlite"),
        "HERMES_TASK_STATE_FILE": str(home / "tasks.sqlite"),
        "HERMES_STATE_FILE": str(home / "platform.sqlite"),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
    }
    command = [
        "/home/h3c/anaconda3/envs/scat-hermes/bin/python",
        "-B",
        str(ROOT / "scripts/run_hermes_api.py"),
        "--home",
        str(home.relative_to(ROOT)),
        "--port",
        str(port),
        "--with-mcp",
        "--synthetic-mcp",
        "--with-model-tasks",
        "--with-skills",
    ]
    child = None
    try:
        with log_path.open("w") as log:
            log_path.chmod(0o600)
            child = await asyncio.to_thread(
                subprocess.Popen, command, cwd=ROOT, env=env, stdout=log, stderr=log
            )
            async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
                for _ in range(90):
                    if child.poll() is not None:
                        raise RuntimeError("hermes_exited_before_health")
                    try:
                        response = await health.get(origin + "/health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("hermes_health_timeout")
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            registry = RunRegistry(home / "bindings.sqlite", workspace_root=ROOT)
            context = ExecutionContext("scat-researcher", "s2-" + identity)
            results = ToolResultStore(home / "results.sqlite", workspace_root=ROOT)
            async with HermesHTTPRuntime(origin, SecretStr(key), registry) as runtime:
                prompt = (
                    "这是合成协议测试，所有工具数据都是合成数据。按已发布技能查询呼号 TEST123，最多1条。"
                    "先列出技能，加载适合的航班技能和它要求的参考文档，再调用航班查询。"
                    "简要说明实际工具结果；不执行模型任务。"
                )
                start = time.monotonic()
                run = await runtime.create_run(context, prompt, request_id=uuid4())
                completed_tools, events, terminal = [], [], None
                async with asyncio.timeout(240):
                    async for event in runtime.events(context, run["run_id"]):
                        events.append(event.name)
                        if event.name == "tool.completed":
                            completed_tools.append(event.payload.get("tool"))
                        if event.name in {"run.completed", "run.failed", "run.cancelled"}:
                            terminal = event.name
                status = await runtime.get_run(context, run["run_id"])
                with sqlite3.connect(home / "skill-loads.sqlite") as db:
                    rows = db.execute(
                        "SELECT skill_id,version,bundle_sha256,file_path,file_sha256 "
                        "FROM skill_loads WHERE scope=? AND run_id=?",
                        (context.scope_key, run["run_id"]),
                    ).fetchall()
                with sqlite3.connect(home / "skill-loads.sqlite") as db:
                    operation_rows = db.execute(
                        "SELECT operation,skill_id,skill_version,bundle_sha256,outcome,"
                        "arguments_sha256,result_sha256,load_id FROM skill_operations "
                        "WHERE scope=? AND run_id=?",
                        (context.scope_key, run["run_id"]),
                    ).fetchall()
                    matching = db.execute(
                        "SELECT count(*) FROM skill_operations o JOIN skill_loads l "
                        "ON o.load_id=l.id AND o.scope=l.scope AND o.run_id=l.run_id "
                        "AND o.session_id=l.session_id AND o.skill_id=l.skill_id "
                        "AND o.skill_version=l.version AND o.bundle_sha256=l.bundle_sha256 "
                        "AND o.file_sha256=l.file_sha256 WHERE o.scope=? AND o.run_id=? "
                        "AND l.file_path='SKILL.md' AND o.outcome='returned'",
                        (context.scope_key, run["run_id"]),
                    ).fetchone()[0]
                if not operation_rows or matching != len(operation_rows):
                    raise RuntimeError("skill_operation_load_link_missing")
                report["skill_operation_receipts"] = operation_rows
                report["matching_load_operation_links"] = matching
                receipts = results.list(context.scope_key, run["run_id"])
                report.update(
                    run_id=run["run_id"],
                    terminal=terminal,
                    run_status=status.get("status"),
                    completed_tools=completed_tools,
                    elapsed_seconds=round(time.monotonic() - start, 2),
                    skill_load_receipts=[
                        dict(
                            zip(
                                ["skill_id", "version", "bundle_sha256", "file", "file_sha256"],
                                row,
                                strict=True,
                            )
                        )
                        for row in rows
                    ],
                    successful_business_receipts=len(receipts),
                    exact_runtime_tools="5 readonly MCP + 5 H5 task reads/prepare + 2 skill reads",
                )
                required = {"skills_list", "skill_view", "mcp__scat__search_scat_flights"}
                if terminal != "run.completed" or status.get("status") != "completed":
                    raise RuntimeError("run_not_completed")
                if not required.issubset(completed_tools) or "tool.failed" in events:
                    raise RuntimeError("required_tools_not_completed")
                if not any(row[0] == "scat-flight-query" and row[3] == "SKILL.md" for row in rows):
                    raise RuntimeError("trusted_skill_receipt_missing")
                if not any(row[3] == "references/query-contract.md" for row in rows):
                    raise RuntimeError("trusted_reference_receipt_missing")
                if not any(x["operation"] == "search_scat_flights" for x in receipts):
                    raise RuntimeError("successful_synthetic_business_receipt_missing")
                query = results.get(context.scope_key, run["run_id"], receipts[-1]["result_id"])
                assert query["result"]["status"] == "ok"
                report.update(
                    status="passed",
                    real_database_query=False,
                    synthetic=True,
                    real_model=True,
                    turn_budget=8,
                    budget_note="single bounded real-model run; no GPU execution",
                )
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
            report["child_stopped"] = child.poll() is not None
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()
        report["log"] = str(log_path.relative_to(ROOT))


if __name__ == "__main__":
    report = {"status": "running", "started_at": time.time()}
    try:
        asyncio.run(probe(report))
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AssertionError,
        TimeoutError,
        httpx.HTTPError,
    ) as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, RuntimeError) and str(exc) in {
            "probe_port_in_use",
            "hermes_exited_before_health",
            "hermes_health_timeout",
            "run_not_completed",
            "required_tools_not_completed",
            "trusted_skill_receipt_missing",
            "trusted_reference_receipt_missing",
            "successful_synthetic_business_receipt_missing",
            "skill_operation_load_link_missing",
        }:
            report["error_code"] = str(exc)
    report["finished_at"] = time.time()
    path = ROOT / "docs/validation/20261023-S2-real.json"
    if path.exists():
        path = path.with_name("20261023-S2-real-" + uuid4().hex[:8] + ".json")
    report["evidence"] = str(path.relative_to(ROOT))
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
