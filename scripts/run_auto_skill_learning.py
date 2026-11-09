"""Persistent finite scheduling of verified experiences into native author drafts and trials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
NATIVE = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


async def execute(ledger, job, policy):
    import httpx

    from application.context import ExecutionContext
    from hermes_runtime.authoring_store import AuthoringStore
    from hermes_runtime.autonomy_config import private_text
    from hermes_runtime.experience_learning import verify_automatic_content
    from hermes_runtime.skill_learning import SkillLearningStore

    evidence = job["evidence"]
    authoring = AuthoringStore(ROOT, policy.author_state)
    learning = SkillLearningStore(authoring)
    candidate = learning.create(
        policy.owner,
        "从服务端核验的成功经验总结可复用技能，保留输入约束、失败分支和验收。",
        evidence,
    )
    context = ExecutionContext(policy.owner, "automatic-author-" + job["id"])
    permanent = ROOT / "var/autonomy/author" / job["id"]
    permanent.mkdir(parents=True, exist_ok=True)
    binding = permanent / "binding.json"
    binding.write_text(json.dumps({"candidate": candidate["id"], "scope": context.scope_key}))
    binding.chmod(0o600)
    home = ROOT / ".tmp/autonomy" / ("author-" + job["id"])
    home.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / "var/validation/20261028-autonomy" / ("author-" + job["id"] + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ledger.update(job["id"], "authoring", candidate=candidate["id"])
    child = None
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 18144))
        env = os.environ | {
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(home),
            "TEMP": str(home),
            "TMP": str(home),
            "SCAT_BOUNDED_MODEL_ROLE": "author",
            "SCAT_SKILL_RELEASE_STATE": policy.author_state,
            "SCAT_AUTHOR_KEY_FILE": policy.author_key,
            "HERMES_API_KEY_FILE": "var/deploy/autonomy/secrets/native-author.key",
            "SCAT_HERMES_PROFILE_ROOT": "var/autonomy/profiles",
        }
        env.pop("SCAT_AUTONOMY_CONFIG", None)
        with log_path.open("a") as stream:
            child = await asyncio.to_thread(
                subprocess.Popen,
                [
                    NATIVE,
                    "-B",
                    "scripts/run_hermes_api.py",
                    "--profile",
                    "scat-author",
                    "--home",
                    str(home.relative_to(ROOT)),
                    "--port",
                    "18144",
                    "--author-api-url",
                    policy.author_url,
                    "--autonomy-job",
                    str(binding.relative_to(ROOT)),
                    "--readonly-budget",
                    json.dumps(
                        {
                            "max_turns": 12,
                            "max_tool_calls": 20,
                            "max_llm_tokens": 750000,
                            "wall_seconds": 240,
                        }
                    ),
                ],
                cwd=ROOT,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        async with httpx.AsyncClient(trust_env=False, timeout=60) as http:
            for _ in range(120):
                if child.poll() is not None:
                    raise RuntimeError("automatic_author_process_exited")
                try:
                    if (await http.get("http://127.0.0.1:18144/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("automatic_author_unavailable")
            headers = {
                "Authorization": "Bearer "
                + private_text(ROOT, "var/deploy/autonomy/secrets/native-author.key"),
                "X-Hermes-Session-Key": context.scope_key,
                "Idempotency-Key": context.scope_key + ":" + str(UUID(job["id"])),
            }
            prompt = (
                "本次仅处理经验候选 " + candidate["id"] + "。先用learning_read读取并核对SHA，"
                "参考catalog/published工具了解已注册操作和契约。生成一项新的可复用技能（名称auto-开头），"
                "只允许候选中已验证的业务操作，不编造数据或授予执行权限。正文必须有YAML name/description，"
                "以及十个二级标题：适用条件、必要参数、输入输出、前置条件、允许操作、执行流程、完成条件、失败处理、示例、评测。"
                "content必须是{changes:[{name,version,allowed_operations,requires_skills:{},contract:{version:1,inputs:[已注册输入契约],outputs:[已注册输出契约]},files:{'SKILL.md':完整正文}}]}。"
                "按真实回执流程写实用步骤、明确必须由用户提供的参数、失败停止和复核方法。不得复制真实业务值。"
                "用learning_draft和候选SHA保存，随后validate；若校验失败，只修订该草稿再校验。"
                "隔离试跑由可信调度器执行；你无需试跑、发布或等待用户授权。完成后简要返回真实草稿ID。"
            )
            # Mark before HTTP: uncertain POST is never silently retried on daemon restart.
            ledger.update(job["id"], "unknown")
            response = await http.post(
                "http://127.0.0.1:18144/v1/runs", headers=headers, json={"input": prompt}
            )
            response.raise_for_status()
            run = response.json()["run_id"]
            ledger.update(job["id"], "authoring", native_run=run)
            deadline = time.monotonic() + 650
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                try:
                    result = await http.get(
                        "http://127.0.0.1:18144/v1/runs/" + run, headers=headers
                    )
                except httpx.ReadTimeout:
                    continue  # Read-only polling may repeat; the run POST never repeats.
                result.raise_for_status()
                data = result.json()
                if data.get("status") in {"failed", "cancelled", "interrupted"}:
                    raise RuntimeError("automatic_author_failed")
                if data.get("status") == "completed":
                    if (
                        data.get("partial")
                        or data.get("interrupted")
                        or data.get("completed") is False
                    ):
                        raise RuntimeError("automatic_author_incomplete")
                    break
            else:
                raise RuntimeError("automatic_author_deadline")
        generated = learning.get(policy.owner, candidate["id"])
        if not generated["draft"]:
            raise RuntimeError("automatic_author_no_draft")
        draft = authoring.get(policy.owner, generated["draft"])
        verify_automatic_content(generated, draft["content"])
        authoring.candidate(draft)  # Independent deterministic validation of actual saved content.
        ledger.update(job["id"], "validated", draft=generated["draft"])
    except Exception as exc:
        current = ledger.get(job["id"])
        ledger.update(
            job["id"],
            "unknown" if current["state"] == "unknown" else "failed",
            error=type(exc).__name__ + ":automatic_author_stage_failed",
        )
        raise
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)
        if home.parent.resolve() == (ROOT / ".tmp/autonomy").resolve() and not home.is_symlink():
            # Retain actual native model/tool-call proof, never temporary credentials.
            for name in ("author-budget.sqlite", "state.db"):
                source, target = home / name, permanent / name
                if (
                    source.is_file()
                    and not source.is_symlink()
                    and source.stat().st_size <= 8 * 1024**2
                ):
                    with sqlite3.connect(source) as original, sqlite3.connect(target) as archive:
                        original.backup(archive)
                    target.chmod(0o600)
            shutil.rmtree(home)
    if not policy.auto_trials:
        return
    trial = authoring.begin_trial(policy.owner, generated["draft"])
    ledger.update(job["id"], "trial_running", trial=trial["id"])
    # Existing holdout runner validates related domains three times and retains all failures.
    trial_env = os.environ | {
        "SCAT_BOUNDED_MODEL_ROLE": "trial",
        "SCAT_SKILL_RELEASE_STATE": policy.author_state,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    trial_env.pop("SCAT_AUTONOMY_CONFIG", None)
    with log_path.open("a") as stream:
        process = await asyncio.create_subprocess_exec(
            APP,
            "-B",
            "scripts/run_skill_author_trial.py",
            "--actor",
            policy.owner,
            "--trial",
            trial["id"],
            cwd=ROOT,
            env=trial_env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=3600)
        except BaseException:
            process.terminate()
            await process.wait()
            ledger.update(job["id"], "failed", error="isolated_trial_interrupted")
            raise
    finished = authoring.trial(policy.owner, trial["id"])
    ledger.update(
        job["id"], "ready_for_review" if finished["state"] == "passed" else "trial_failed"
    )


async def main(once):
    import fcntl

    from hermes_runtime.autonomy_config import load_autonomy
    from hermes_runtime.experience_learning import (
        LearningLedger,
        collect_readonly,
        collect_workflows,
    )

    policy = load_autonomy(ROOT).learning
    ledger = LearningLedger(ROOT, policy)
    with (ledger.path.parent / "learning.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A crashed owner cannot safely repeat a model/tool dispatch.
        with ledger.connect() as db:
            db.execute(
                "UPDATE automatic_skill_jobs SET state='unknown',error='scheduler_restart_requires_reconciliation' WHERE state IN ('authoring','trial_running','reserved')"
            )
        while True:
            if policy.enabled:
                await collect_readonly(ROOT, policy, ledger)
                collect_workflows(ROOT, policy, ledger)
                job = ledger.reserve()
                if job:
                    try:
                        await execute(ledger, job, policy)
                    except Exception as exc:  # noqa: BLE001 -- retain status, continue receiving new sources
                        print(
                            json.dumps({"job": job["id"], "error_type": type(exc).__name__}),
                            flush=True,
                        )
            if once:
                break
            await asyncio.sleep(30)


if __name__ == "__main__":
    if sys.executable != APP:
        raise RuntimeError("existing_APP_Anaconda_required")
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
