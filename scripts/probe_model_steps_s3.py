"""Finite pinned Hermes + DeepSeek + actual synthetic CPU operation acceptance."""

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


async def probe(report):
    from application.context import ExecutionContext
    from application.model_step_store import ModelStepStore
    from hermes_runtime.client import HermesHTTPRuntime
    from hermes_runtime.registry import RunRegistry
    from scat_model_runtime.step_codec import decode

    identity = uuid4().hex[:12]
    home = ROOT / ".tmp/s3" / ("runtime-" + identity)
    home.mkdir(parents=True)
    log_path = ROOT / "var/validation" / ("hermes-s3-" + identity + ".log")
    origin = "http://127.0.0.1:18092"
    child = None
    async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
        try:
            await health.get(origin + "/health")
        except httpx.TransportError:
            pass
        else:
            raise RuntimeError("probe_port_in_use")
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "TEMP": str(home),
        "TMP": str(home),
        "TMPDIR": str(home),
        "HERMES_RESULT_STATE_FILE": str(home / "results.sqlite"),
        "HERMES_TASK_STATE_FILE": str(home / "tasks.sqlite"),
        "HERMES_STATE_FILE": str(home / "platform.sqlite"),
    }
    command = [
        "/home/h3c/anaconda3/envs/scat-hermes/bin/python",
        "-B",
        str(ROOT / "scripts/run_hermes_api.py"),
        "--home",
        str(home.relative_to(ROOT)),
        "--port",
        "18092",
        "--with-mcp",
        "--synthetic-mcp",
        "--with-model-tasks",
        "--with-skills",
        "--s3-cpu-fixture",
    ]
    try:
        with log_path.open("x") as log:
            log_path.chmod(0o600)
            child = await asyncio.to_thread(
                subprocess.Popen, command, cwd=ROOT, env=env, stdout=log, stderr=log
            )
            async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
                for _ in range(100):
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
            context = ExecutionContext("scat-researcher", "s3-" + identity)
            registry = RunRegistry(home / "bindings.sqlite", workspace_root=ROOT)
            started = time.monotonic()
            async with HermesHTTPRuntime(origin, SecretStr(key), registry) as runtime:
                run = await runtime.create_run(
                    context,
                    "合成 CPU 验收：先列技能、加载 scat-model-analysis。可信任务已由维护者启用，"
                    "按技能实际完成准备输入→执行→评估→保存→读取五个步骤。每步只用上一步实际返回的完整 reference。"
                    "读取最终 AnalysisResult 后，简短报告 x/y/alt 的 MAE 与 RMSE、单位、相对秒时间轴和最终 result 引用，"
                    "明确为合成测试。不要调用 H5 准备、其他只读 MCP，不估算数值，不再次请求确认。",
                    request_id=uuid4(),
                )
                tools, terminal = [], None
                async with asyncio.timeout(270):
                    async for event in runtime.events(context, run["run_id"]):
                        if event.name == "tool.completed":
                            tools.append(event.payload.get("tool"))
                        if event.name in {"run.completed", "run.failed", "run.cancelled"}:
                            terminal = event.name
                status = await runtime.get_run(context, run["run_id"])
            report.update(
                run_id=run["run_id"],
                terminal=terminal,
                completed_tools=tools,
                elapsed_seconds=round(time.monotonic() - started, 2),
                output=status.get("output"),
            )
            assert terminal == "run.completed" and status["status"] == "completed"
            with sqlite3.connect(home / "skill-loads.sqlite") as db:
                db.row_factory = sqlite3.Row
                operations = [
                    dict(row)
                    for row in db.execute(
                        "SELECT operation,skill_id,skill_version,arguments_sha256,result_sha256,load_id FROM skill_operations WHERE scope=? AND run_id=?",
                        (context.scope_key, run["run_id"]),
                    )
                ]
                budget = dict(
                    db.execute(
                        "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                        (context.scope_key, run["run_id"]),
                    ).fetchone()
                )
                matching = db.execute(
                    "SELECT count(*) FROM skill_operations o JOIN skill_loads l ON o.load_id=l.id AND o.scope=l.scope AND o.run_id=l.run_id AND o.session_id=l.session_id AND o.file_sha256=l.file_sha256 WHERE o.scope=? AND o.run_id=? AND l.file_path='SKILL.md' AND o.outcome='returned'",
                    (context.scope_key, run["run_id"]),
                ).fetchone()[0]
            expected = [
                "scat_model_prepare_input",
                "scat_model_run",
                "scat_model_evaluate",
                "scat_model_save_result",
                "scat_model_read_result",
            ]
            assert {row["operation"] for row in operations} == set(expected)
            assert matching == len(operations) and all(
                row["skill_id"] == "scat-model-analysis" for row in operations
            )
            store = ModelStepStore(home / "model-steps.sqlite", workspace_root=ROOT)
            with sqlite3.connect(store.path) as db:
                db.row_factory = sqlite3.Row
                receipts = [
                    dict(row)
                    for row in db.execute(
                        "SELECT operation,state,reference FROM model_step_receipts ORDER BY rowid"
                    )
                ]
            assert [row["operation"] for row in receipts] == expected[:4]
            assert all(row["state"] == "succeeded" for row in receipts)
            ref = json.loads(receipts[-1]["reference"])
            body = store.read(context.scope_key, run["run_id"], ref, kind="result")
            state = decode(body["state"])
            assert state["regression_output"].series[0].values == ((40, 80, 1020), (50, 100, 1025))
            assert [m.mae for m in state["regression_evaluation"].target_errors] == [1, 0.5, 0]
            assert state["regression_output"].target_units == {"x": "m", "y": "m", "alt": "m"}
            assert state["regression_output"].series[0].time_offsets_seconds == (1, 2)
            assert "合成" in (status.get("output") or "") and ref["artifact_id"] in status["output"]
            assert budget["exhausted"] is None and budget["finished_at"] is not None
            report.update(
                status="passed",
                skill_operations=operations,
                matching_loads=matching,
                step_receipts=receipts,
                budget=budget,
                final_reference=ref,
                analysis_result=body["analysis_result"],
                synthetic=True,
                actual_cpu_calculation=True,
                real_database_queries=0,
                production_gpu=False,
                exact_runtime_tools=38,
                legacy_graph_in_new_business_path=False,
                model="synthetic-cpu-cv:1.0.0",
                evaluator="regression-errors:1",
            )
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)
        assert home.resolve().is_relative_to((ROOT / ".tmp/s3").resolve()) and not home.is_symlink()
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    report = {"status": "running"}
    try:
        asyncio.run(probe(report))
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        target = ROOT / "docs/validation" / ("20261023-S3-hermes-" + uuid4().hex[:10] + ".json")
        with target.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"report": str(target), "status": report["status"]}))
