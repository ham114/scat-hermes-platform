"""Finite natural-language repair check: real Hermes/LLM, synthetic business only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application.context import ExecutionContext
from application.readonly_display import receipt_display
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.registry import RunRegistry
from hermes_runtime.results import ToolResultStore

OUT = ROOT / "var/validation/20261107-query-display-repair"
PORT = 18154


async def main(report):
    identity = uuid4().hex[:12]
    # The existing bounded trial-role runtime enforces this prefix. No policy bypass.
    home = ROOT / ".tmp/s7a" / ("query-display-" + identity)
    home.mkdir(parents=True, exist_ok=False)
    report["temporary_home"] = home.relative_to(ROOT).as_posix()
    origin = f"http://127.0.0.1:{PORT}"
    child = None
    limits = {"max_turns": 8, "max_llm_tokens": 500000, "max_tool_calls": 24, "wall_seconds": 240}
    report["limits_per_run"] = limits
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "TMPDIR": str(home),
        "TMP": str(home),
        "TEMP": str(home),
        "HERMES_STATE_FILE": str(home / "bindings.sqlite"),
        "HERMES_TASK_STATE_FILE": str(home / "tasks.sqlite"),
        "HERMES_RESULT_STATE_FILE": str(home / "results.sqlite"),
        "SCAT_BOUNDED_MODEL_ROLE": "trial",
        "SCAT_S5_SYNTHETIC": "1",
        "SCAT_GBAR_V23_ENABLED": "false",
        "DEFAULT_MODEL": "fake",
    }
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            try:
                await client.get(origin + "/health")
            except httpx.TransportError:
                pass
            else:
                raise RuntimeError("isolated_port_in_use")
        log_path = OUT / ("natural-" + identity + ".log")
        report["log"] = log_path.relative_to(ROOT).as_posix()
        with log_path.open("x") as log:
            log_path.chmod(0o600)
            child = await asyncio.to_thread(
                subprocess.Popen,
                [
                    "/home/h3c/anaconda3/envs/scat-hermes/bin/python",
                    "-B",
                    str(ROOT / "scripts/run_hermes_api.py"),
                    "--home",
                    str(home.relative_to(ROOT)),
                    "--port",
                    str(PORT),
                    "--with-mcp",
                    "--synthetic-mcp",
                    "--with-skills",
                    "--with-model-tasks",
                    "--with-workflows",
                    "--readonly-budget",
                    json.dumps(limits),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
            )
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                for _ in range(100):
                    if child.poll() is not None:
                        raise RuntimeError("candidate_exited_before_health")
                    try:
                        if (await client.get(origin + "/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("candidate_health_timeout")
            registry = RunRegistry(home / "bindings.sqlite", workspace_root=ROOT)
            results = ToolResultStore(home / "results.sqlite", workspace_root=ROOT)
            thread = registry.create_thread("scat-researcher")
            context = ExecutionContext("scat-researcher", thread["thread_id"])
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            prompts = [
                "绘制 SCAT 航班 100002 的二维轨迹图。",
                "你目前支持哪些预测模型？",
            ]
            if report.get("catalog_only"):
                prompts = prompts[-1:]
            async with HermesHTTPRuntime(origin, SecretStr(key), registry) as runtime:
                for index, prompt in enumerate(prompts):
                    row = {"index": index + 1, "prompt": prompt, "status": "running"}
                    report["attempts"].append(row)
                    run = await runtime.create_run(context, prompt, request_id=uuid4())
                    row["run_id"] = run["run_id"]
                    row["completed_tools"] = []
                    async with asyncio.timeout(250):
                        async for event in runtime.events(context, run["run_id"]):
                            if event.name == "tool.completed":
                                row["completed_tools"].append(event.payload.get("tool"))
                    state = await runtime.get_run(context, run["run_id"])
                    row["status"] = state.get("status")
                    row["output"] = state.get("output")
                    refs = results.list(context.scope_key, run["run_id"])
                    row["successful_receipt_operations"] = [r["operation"] for r in refs]
                    with sqlite3.connect(home / "skill-loads.sqlite") as db:
                        row["skill_loads"] = [
                            r[0]
                            for r in db.execute(
                                "SELECT skill_id FROM skill_loads WHERE run_id=? AND file_path='SKILL.md'",
                                (run["run_id"],),
                            )
                        ]
                        row["operations"] = [
                            r[0]
                            for r in db.execute(
                                "SELECT operation FROM skill_operations WHERE run_id=?",
                                (run["run_id"],),
                            )
                        ]
                        usage = db.execute(
                            "SELECT llm_calls,tool_calls,charged_tokens,exhausted,provider_input_tokens,provider_output_tokens FROM skill_run_budgets WHERE run_id=?",
                            (run["run_id"],),
                        ).fetchone()
                        row["usage"] = dict(
                            zip(
                                [
                                    "llm_calls",
                                    "tool_calls",
                                    "conservative_units",
                                    "exhausted",
                                    "provider_input_tokens",
                                    "provider_output_tokens",
                                ],
                                usage,
                                strict=True,
                            )
                        )
                    row["trajectory_display"] = any(
                        "analysis_result"
                        in receipt_display(
                            results.get(context.scope_key, run["run_id"], r["result_id"])
                        )
                        for r in refs
                        if r["operation"] == "get_scat_track_points"
                    )
                    (OUT / "natural-check.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2)
                    )
                    print(
                        json.dumps(
                            {"index": index + 1, "status": row["status"], "usage": row["usage"]}
                        ),
                        flush=True,
                    )
                    if row["status"] != "completed":
                        raise RuntimeError("natural_run_not_completed")
            if not report.get("catalog_only"):
                assert report["attempts"][0]["trajectory_display"]
            assert "scat_workflow_catalog" in report["attempts"][-1]["completed_tools"]
            assert all(
                name in report["attempts"][-1]["output"]
                for name in ["constant-velocity-heading", "kalman-cv"]
            )
            report["status"] = "passed"
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)
        report["child_stopped"] = child is None or child.poll() is not None
        trace = home / "skill-loads.sqlite"
        if trace.exists():
            saved = OUT / ("natural-" + identity + "-traces.sqlite")
            with sqlite3.connect(trace) as source, sqlite3.connect(saved) as target:
                source.backup(target)
            saved.chmod(0o600)
            report["server_private_traces"] = saved.relative_to(ROOT).as_posix()
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-only", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "synthetic_business": True,
        "prediction_calls": 0,
        "global_budget_proxy": "existing_trial_role",
        "attempts": [],
        "max_runs": 1 if args.catalog_only else 2,
        "catalog_only": args.catalog_only,
    }
    try:
        asyncio.run(main(report))
    except Exception as exc:  # noqa: BLE001 -- never persist credentials or raw transport errors.
        report.update(status="failed", error_type=type(exc).__name__)
    (OUT / "natural-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "attempts": len(report["attempts"])}))
    raise SystemExit(0 if report["status"] == "passed" else 1)
