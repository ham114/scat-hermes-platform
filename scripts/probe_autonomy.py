"""Finite real-provider acceptance: isolated main API memory and verified learning sources."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
NATIVE = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


async def main():
    import httpx
    from dotenv import dotenv_values

    from hermes_runtime.autonomy_config import load_autonomy
    from hermes_runtime.experience_learning import LearningLedger, collect_readonly

    identity = uuid4().hex[:10]
    home = ROOT / ".tmp/s7a" / ("autonomy-" + identity)
    home.mkdir(parents=True)
    logs = ROOT / "var/validation/20261028-autonomy"
    report_path = logs / ("memory-native-" + identity + ".json")
    report = {
        "id": identity,
        "status": "running",
        "synthetic_only": True,
        "attempts": [],
        "checks": {},
    }
    children = []

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        report_path.chmod(0o600)

    for port in (18145, 18146):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    policy = load_autonomy(ROOT, "var/deploy/autonomy/config.json")
    data = policy.model_dump()
    data["memory"].update(
        state=str((home / "memory.sqlite").relative_to(ROOT)),
        identity_key=str((home / "identity.key").relative_to(ROOT)),
    )
    data["learning"]["enabled"] = False
    (home / "identity.key").write_text(secrets.token_urlsafe(48))
    (home / "identity.key").chmod(0o600)
    memory_workspace = (
        "scat-"
        + hmac.new(
            (home / "identity.key").read_text().encode(), b"scat-researcher", hashlib.sha256
        ).hexdigest()
        + "-1"
    )
    report["memory_workspace"] = memory_workspace
    (home / "policy.json").write_text(json.dumps(data))
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(home),
        "TMP": str(home),
        "TMPDIR": str(home),
        "SCAT_AUTONOMY_CONFIG": str((home / "policy.json").relative_to(ROOT)),
        "HERMES_BASE_URL": "http://127.0.0.1:18146",
        "HERMES_API_KEY_FILE": str((home / "native.key").relative_to(ROOT)),
        "HERMES_STATE_FILE": str((home / "bindings.sqlite").relative_to(ROOT)),
        "HERMES_RESULT_STATE_FILE": str((home / "results.sqlite").relative_to(ROOT)),
        "HERMES_TASK_STATE_FILE": str((home / "tasks.sqlite").relative_to(ROOT)),
        "SCAT_SKILL_RELEASE_STATE": policy.learning.author_state,
        "SCAT_HERMES_PROFILE_ROOT": str((home / "profiles").relative_to(ROOT)),
        "SCAT_BOUNDED_MODEL_ROLE": "trial",
        "SCAT_S5_SYNTHETIC": "1",
        "ANALYSIS_IDENTITY_MODE": "single_user",
        "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
        "SCAT_GBAR_V23_ENABLED": "false",
        "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
        "HERMES_SKILL_JOBS_ENABLED": "false",
        "HERMES_WORKFLOWS_ENABLED": "false",
    }
    native_env = dict(env)
    native_env.pop("SCAT_AUTONOMY_CONFIG")

    async def launch(command, name, url, child_env):
        with (logs / (identity + "-" + name + ".log")).open("a") as stream:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=ROOT, env=child_env, stdout=stream, stderr=subprocess.STDOUT
            )
        children.append(process)
        async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
            for _ in range(120):
                if process.returncode is not None:
                    raise RuntimeError(name + "_exited")
                try:
                    if (await http.get(url)).status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise RuntimeError(name + "_unavailable")

    async def stop(process):
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()

    api_command = [APP, "-B", "scripts/run_platform_api.py", "--port", "18145"]
    try:
        with (logs / (identity + "-profiles.log")).open("a") as stream:
            initialization = await asyncio.create_subprocess_exec(
                NATIVE,
                "-B",
                "scripts/profile_manage.py",
                "init",
                "--root",
                str((home / "profiles").relative_to(ROOT)),
                cwd=ROOT,
                env=native_env,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            if await initialization.wait():
                raise RuntimeError("acceptance_profile_initialization_failed")
        await launch(
            [
                NATIVE,
                "-B",
                "scripts/run_hermes_api.py",
                "--profile",
                "scat-main",
                "--home",
                str((home / "main").relative_to(ROOT)),
                "--port",
                "18146",
                "--with-skills",
                "--with-mcp",
                "--synthetic-mcp",
            ],
            "native",
            "http://127.0.0.1:18146/health",
            native_env,
        )
        api = await launch(api_command, "api", "http://127.0.0.1:18145/health/ready", env)
        values = dotenv_values(ROOT / ".env")
        headers = (
            {"Authorization": "Bearer " + values["AUTH_SECRET"]}
            if values.get("AUTH_SECRET")
            else {}
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18145", trust_env=False, headers=headers, timeout=60
        ) as http:

            async def run(prompt):
                thread = await http.post("/api/v1/hermes/threads", json={})
                thread.raise_for_status()
                thread_id = thread.json()["thread_id"]
                endpoint = "/api/v1/hermes/threads/" + thread_id + "/runs"
                request_id = str(uuid4())
                response = await http.post(
                    endpoint, json={"request_id": request_id, "message": prompt}
                )
                response.raise_for_status()
                run_id = response.json()["run_id"]
                entry = {"thread": thread_id, "run": run_id, "status": "running"}
                report["attempts"].append(entry)
                save()
                for _ in range(260):
                    await asyncio.sleep(1)
                    try:
                        response = await http.get(endpoint + "/" + run_id)
                    except httpx.ReadTimeout:
                        continue  # Repeat only the status GET, never the run POST.
                    response.raise_for_status()
                    result = response.json()
                    if result.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                        entry.update(status=result["status"], output=result.get("output"))
                        save()
                        if result["status"] != "completed" or result.get("partial"):
                            raise RuntimeError("acceptance_native_run_failed")
                        return result
                raise RuntimeError("acceptance_native_deadline")

            await run(
                "这是合成偏好验收。请记住我的偏好：回答用简体中文，尽量简短，距离单位优先用海里。只简短确认，不查询数据。"
            )
            for _ in range(180):
                await asyncio.sleep(1)
                context = await http.get("/api/v1/hermes/memory/context")
                context.raise_for_status()
                if "海里" in context.json().get("context", ""):
                    break
            else:
                raise RuntimeError("honcho_derivation_deadline")
            report["checks"]["honcho_real_derivation"] = True
            result = await run("我的回答语言和距离单位偏好是什么？请依据已有记忆回答。只说偏好。")
            assert "海里" in str(result["output"]), "native_recall_missing"
            report["checks"]["cross_thread_main_agent_recall"] = True
            await stop(api)
            api = await launch(
                api_command, "api-restart", "http://127.0.0.1:18145/health/ready", env
            )
            context = await http.get("/api/v1/hermes/memory/context")
            assert "海里" in context.json().get("context", ""), "restart_memory_missing"
            report["checks"]["platform_restart_preserved_memory"] = True
            correction = {
                "request_id": str(uuid4()),
                "content": "更正之前的距离单位偏好：以后统一使用公里，覆盖旧的海里；仍用简短简体中文。",
            }
            response = await http.post("/api/v1/hermes/memory/corrections", json=correction)
            response.raise_for_status()
            replay = await http.post("/api/v1/hermes/memory/corrections", json=correction)
            assert replay.json()["replayed"], "correction_not_idempotent"
            result = await run("我现在最新的距离单位偏好是什么？只说当前单位。")
            assert "公里" in str(result["output"]), "corrected_recall_missing"
            report["checks"]["latest_correction_and_replay"] = True
            # Real native MCP receipts with distinct query inputs; never fabricate success rows.
            successes = 0
            for limit in range(1, 6):
                try:
                    await run(
                        "合成只读学习验收：先加载scat-flight-query，实际调用search_scat_flights查询批次SYNTH-S6-HOLDOUT、呼号QA601，limit="
                        + str(limit)
                        + "。只做这一个业务查询，报告实际条数和来源；不查其他工具，不准备任务。"
                    )
                    successes += 1
                except RuntimeError:
                    pass
                if successes >= 3:
                    break
            source_policy = policy.learning.model_copy(
                update={
                    "registry": str((home / "bindings.sqlite").relative_to(ROOT)),
                    "results": str((home / "results.sqlite").relative_to(ROOT)),
                    "traces": str((home / "main/skill-loads.sqlite").relative_to(ROOT)),
                    "native_url": "http://127.0.0.1:18146",
                    "native_key": str((home / "native.key").relative_to(ROOT)),
                }
            )
            ledger = LearningLedger(ROOT, policy.learning)
            collected = await collect_readonly(ROOT, source_policy, ledger)
            assert collected >= 3, "verified_sources_insufficient"
            report["checks"]["verified_native_sources"] = collected
            # Retain private bounded receipt/trace proof for source SHA re-audit after temp cleanup.
            for source in (home / "results.sqlite", home / "main/skill-loads.sqlite"):
                with (
                    sqlite3.connect(source) as original,
                    sqlite3.connect(logs / (identity + "-" + source.name)) as target,
                ):
                    original.backup(target)
                (logs / (identity + "-" + source.name)).chmod(0o600)
            report["learning"] = ledger.status(policy.learning.owner)
            response = await http.delete("/api/v1/hermes/memory")
            assert response.status_code == 202 and not response.json()["enabled"]
            for _ in range(60):
                await asyncio.sleep(1)
                status = (await http.get("/api/v1/hermes/memory")).json()
                if (
                    status["queue"].get("pending", 0) == 0
                    and status["queue"].get("sending", 0) == 0
                ):
                    break
            else:
                raise RuntimeError("memory_deletion_deadline")
            context = await http.get("/api/v1/hermes/memory/context")
            assert context.json() == {"enabled": False, "context": ""}
            report["checks"]["forget_disabled_and_deleted_workspace"] = True
            report["status"] = "passed"
    except BaseException as exc:
        report.update(
            status="failed",
            error_type=type(exc).__name__,
            error_code=str(exc)
            if isinstance(exc, (AssertionError, RuntimeError))
            else "acceptance_stage_failed",
        )
        raise
    finally:
        for process in reversed(children):
            await stop(process)
        from hermes_runtime.autonomy_config import private_text
        from hermes_runtime.honcho_memory import HonchoClient

        cleanup = HonchoClient(
            policy.memory.base_url, private_text(ROOT, policy.memory.signing_key)
        )
        try:
            for _ in range(30):
                if await cleanup.delete(memory_workspace):
                    report["canary_workspace_removed"] = True
                    break
                await asyncio.sleep(1)
        except (httpx.HTTPError, ValueError):
            report["canary_workspace_removed"] = False
        finally:
            await cleanup.http.aclose()
        if home.parent.resolve() == (ROOT / ".tmp/s7a").resolve() and not home.is_symlink():
            shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()
        report["owned_processes_remaining"] = sum(
            process.returncode is None for process in children
        )
        save()
        print(
            json.dumps(
                {
                    "report": str(report_path.relative_to(ROOT)),
                    "status": report["status"],
                    "checks": report["checks"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    if sys.executable != APP:
        raise RuntimeError("existing_APP_Anaconda_required")
    os.umask(0o077)
    asyncio.run(main())
