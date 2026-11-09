"""One finite real Hermes/DeepSeek synthetic S2 acceptance case; no business data."""

from __future__ import annotations

import argparse
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


async def probe(case, report):
    from application.context import ExecutionContext
    from hermes_runtime.client import HermesHTTPRuntime
    from hermes_runtime.readonly_contracts import resolve_reference, result_reference
    from hermes_runtime.registry import RunRegistry
    from hermes_runtime.results import ToolResultStore
    from hermes_runtime.skill_policy import load_policy
    from hermes_runtime.skill_templates import load_investigation
    from hermes_runtime.skills import SkillRelease

    identity = uuid4().hex[:12]
    home = ROOT / ".tmp/s2c" / ("runtime-" + identity)
    home.mkdir(parents=True)
    origin = "http://127.0.0.1:18091"
    log_path = ROOT / "var/validation" / ("hermes-s2c-" + identity + ".log")
    child = None
    async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
        try:
            await health.get(origin + "/health")
        except httpx.TransportError:
            pass
        else:
            raise RuntimeError("probe_port_in_use")
    release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
    policy = load_policy(ROOT)
    template = None
    if case in {"full", "reuse"}:
        name = "a" if case == "full" else "b"
        template = load_investigation(ROOT / f"config/investigation-synthetic-{name}.json").prepare(
            release, policy
        )
        prompt = "以下全部是合成测试资料，请显式标注合成。" + template["prompt"]
        overrides = template["template"]["budget"]
    elif case == "uav":
        prompt = "合成测试：先 skills_list，再加载 uav-flight-analysis 正文，实际 list_uav_models；对给定合成架次 SYNTH-UAV-001 调用 get_uav_samples 最多2个样本，通道 cmp_comb_alt。简短用中文说明实际样本数和通道值；不执行模型或其他业务。"
        overrides = {}
    else:
        prompt = "合成测试：查询 SYNTH 批次一个航班。"
        overrides = {"max_llm_tokens": 1}
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
        "18091",
        "--with-mcp",
        "--synthetic-mcp",
        "--with-model-tasks",
        "--with-skills",
        "--readonly-budget",
        json.dumps(overrides),
    ]
    try:
        with log_path.open("w") as log:
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
            context = ExecutionContext("scat-researcher", "s2c-" + identity)
            registry = RunRegistry(home / "bindings.sqlite", workspace_root=ROOT)
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            async with HermesHTTPRuntime(origin, SecretStr(key), registry) as runtime:
                started = time.monotonic()
                run = await runtime.create_run(context, prompt, request_id=uuid4())
                tools, terminal = [], None
                async with asyncio.timeout(270):
                    async for event in runtime.events(context, run["run_id"]):
                        if event.name == "tool.completed":
                            tools.append(event.payload.get("tool"))
                        if event.name in {"run.completed", "run.failed", "run.cancelled"}:
                            terminal = event.name
                status = await runtime.get_run(context, run["run_id"])
            with sqlite3.connect(home / "skill-loads.sqlite") as db:
                db.row_factory = sqlite3.Row
                budget = dict(
                    db.execute(
                        "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                        (context.scope_key, run["run_id"]),
                    ).fetchone()
                )
                loads = [
                    dict(row)
                    for row in db.execute(
                        "SELECT skill_id,file_path FROM skill_loads WHERE scope=? AND run_id=?",
                        (context.scope_key, run["run_id"]),
                    )
                ]
                matching = db.execute(
                    "SELECT count(*) FROM skill_operations o JOIN skill_loads l ON o.load_id=l.id AND o.scope=l.scope AND o.run_id=l.run_id AND o.session_id=l.session_id AND o.skill_id=l.skill_id AND o.skill_version=l.version AND o.bundle_sha256=l.bundle_sha256 AND o.file_sha256=l.file_sha256 WHERE o.scope=? AND o.run_id=? AND o.outcome='returned' AND l.file_path='SKILL.md'",
                    (context.scope_key, run["run_id"]),
                ).fetchone()[0]
            report.update(
                terminal=terminal,
                run_status=status["status"],
                completed_tools=tools,
                budget=budget,
                loads=loads,
                matching_operation_loads=matching,
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
            if case == "budget":
                assert terminal == "run.failed" and status["status"] == "failed"
                assert budget["llm_calls"] == budget["tool_calls"] == 0
                assert budget["exhausted"] == "max_llm_tokens"
            else:
                assert terminal == "run.completed" and status["status"] == "completed"
                assert budget["llm_calls"] > 0 and budget["provider_input_tokens"] > 0
                assert budget["exhausted"] is None and budget["finished_at"] is not None
                limits = json.loads(budget["limits_json"])
                assert (
                    budget["llm_calls"] <= limits["max_turns"]
                    and budget["tool_calls"] <= limits["max_tool_calls"]
                )
                assert budget["charged_tokens"] <= limits["max_llm_tokens"]
                store = ToolResultStore(home / "results.sqlite", workspace_root=ROOT)
                receipts = store.list(context.scope_key, run["run_id"])
                payloads = {}
                for row in receipts:
                    typed = result_reference(
                        store, release, context.scope_key, run["run_id"], row["result_id"]
                    )
                    payloads[row["operation"]] = resolve_reference(
                        store,
                        release,
                        context.scope_key,
                        run["run_id"],
                        typed.model_dump(),
                        expected_contract=typed.contract,
                    )
                if template:
                    output = status.get("output") or ""
                    assert "合成" in output
                    assert template["template"]["callsign"] in output
                    bodies = {row["skill_id"] for row in loads if row["file_path"] == "SKILL.md"}
                    assert set(template["selected_skills"]).issubset(bodies)
                    assert any(row["file_path"] == "references/query-contract.md" for row in loads)
                    query = payloads["search_scat_flights"]
                    track = payloads["get_scat_track_points"]
                    assert query["query"]["batch"] == template["template"]["batch"]
                    assert query["query"]["callsign"] == template["template"]["callsign"]
                    source = query["data"]["items"][0]["source"]
                    assert (
                        track["query"]["external_id"] == source["external_id"]
                        and track["query"]["batch"] == source["batch"]
                    )
                    assert track["query"]["limit"] <= template["template"]["track_limit"]
                    if case == "full":
                        assert "SYNTH-QUALITY-001" in output
                        assert (
                            payloads["inspect_scat_collection_data_quality"]["query"]["batch"]
                            == source["batch"]
                        )
                        assert (
                            payloads["search_scat_knowledge"]["data"]["hits"][0]["source_id"]
                            == "SYNTH-QUALITY-001"
                        )
                    else:
                        assert "SYNTH-B" in output
                        assert (
                            "inspect_scat_collection_data_quality" not in payloads
                            and "search_scat_knowledge" not in payloads
                        )
                    assert matching >= (4 if case == "full" else 2)
                else:
                    assert "list_uav_models" in payloads and "get_uav_samples" in payloads
                    samples = payloads["get_uav_samples"]
                    assert samples["query"]["external_id"] == "SYNTH-UAV-001"
                    assert (
                        len(samples["data"]["samples"]) == 2
                        and samples["data"]["samples"][0]["channels"]["cmp_comb_alt"] == 1000
                    )
                    assert matching >= 2
                    output = status.get("output") or ""
                    assert "1000" in output and "1001" in output and "合成" in output
                report["validated_readonly_references"] = len(receipts)
            report.update(
                status="passed",
                real_model=case != "budget",
                synthetic=True,
                real_database_query=False,
                gpu_executed=False,
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
        assert home.resolve().is_relative_to(ROOT / ".tmp/s2c") and not home.is_symlink()
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()
        report["log"] = str(log_path.relative_to(ROOT))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["full", "reuse", "uav", "budget"], required=True)
    args = parser.parse_args()
    report = {"status": "running", "case": args.case, "started_at": time.time()}
    try:
        asyncio.run(probe(args.case, report))
    except (
        RuntimeError,
        ValueError,
        OSError,
        AssertionError,
        KeyError,
        TypeError,
        TimeoutError,
        httpx.HTTPError,
    ) as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, RuntimeError) and str(exc) in {
            "hermes_exited_before_health",
            "hermes_health_timeout",
            "probe_port_in_use",
        }:
            report["error_code"] = str(exc)
    report["finished_at"] = time.time()
    path = (
        ROOT
        / "docs/validation"
        / ("20261023-S2C-real-" + args.case + "-" + uuid4().hex[:8] + ".json")
    )
    report["evidence"] = str(path.relative_to(ROOT))
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
