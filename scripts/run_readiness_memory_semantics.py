"""Synthetic-only real Honcho/Main adoption, correction, isolation and deletion."""

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from application.context import ExecutionContext
from hermes_runtime.autonomy_config import load_autonomy, private_text
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.honcho_memory import MemoryRuntime
from hermes_runtime.registry import RunRegistry


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def budget():
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/validation/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        return db.execute("SELECT count(*),coalesce(sum(units),0) FROM validation_calls").fetchone()


async def main():
    os.umask(0o077)
    assert (
        json.loads((OUT / "proxy-campaign-runtime.json").read_text())["status"]
        == "runtime_attested"
    )
    record = OUT / "memory-semantic.json"
    assert not record.exists(), "already_admitted_do_not_replay"
    assert budget()[0] <= 65
    rows = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    state_dir = ROOT / "var/deploy/h7b/shared/state/readiness-memory"
    state_dir.mkdir(exist_ok=False)
    key = state_dir / "identity.key"
    key.write_text(secrets.token_hex(32) + "\n")
    key.chmod(0o600)
    policy = load_autonomy(ROOT, "var/deploy/autonomy/config.json").memory.model_copy(
        update={
            "state": str((state_dir / "memory.sqlite").relative_to(ROOT)),
            "identity_key": str(key.relative_to(ROOT)),
        }
    )
    memory = MemoryRuntime(ROOT, policy)
    registry = RunRegistry(state_dir / "bindings.sqlite", workspace_root=ROOT)
    runtime = HermesHTTPRuntime(
        "http://127.0.0.1:18121",
        SecretStr(private_text(ROOT, "var/deploy/h7b/secrets/hermes.key")),
        registry,
        memory=memory,
    )
    report = {
        "status": "running",
        "synthetic_preferences_only": True,
        "isolated_identity_key": True,
        "business_memory_modified": False,
        "before_budget": budget(),
        "cases": [],
    }
    save(record, report)
    owner = "scat-admin"
    workspace = memory.workspace(memory.owner(owner), 1)
    memory.start()

    async def run_case(case_id, user, index, message, expected=None, forbidden=None):
        selected = next(r for r in rows if r["user"] == user and r["index"] == index)
        actual_owner = {"admin": "scat-admin", "ZYJ": "scat-zyj"}[user]
        context = ExecutionContext(actual_owner, selected["thread_id"])
        assert context.scope_key == selected["scope"]
        with sqlite3.connect(registry.path) as db:
            db.execute(
                "INSERT INTO runtime_threads VALUES(?,?,?)",
                (context.thread_id, actual_owner, time.time()),
            )
        entry = {
            "id": case_id,
            "thread_id": context.thread_id,
            "request_id": str(uuid4()),
            "status": "admission_pending",
        }
        report["cases"].append(entry)
        save(record, report)
        admission = await runtime.create_run(
            context, message, request_id=__import__("uuid").UUID(entry["request_id"])
        )
        entry.update(run_id=admission["run_id"], status="running")
        save(record, report)
        for _ in range(120):
            await asyncio.sleep(1)
            state = await runtime.get_run(context, admission["run_id"])
            if state["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
        else:
            await runtime.stop_run(context, admission["run_id"])
            raise RuntimeError("native_timeout_stop_requested")
        answer = state.get("output", "")
        save(OUT / "private" / ("memory-" + case_id + ".json"), {"input": message, "state": state})
        checks = {"completed": state["status"] == "completed"}
        if expected:
            checks["adopted_prefix"] = answer.lstrip().startswith(expected)
        if forbidden:
            checks["old_or_foreign_prefix_absent"] = forbidden not in answer
        entry.update(
            status="passed" if all(checks.values()) else "gaps_found",
            checks=checks,
            answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
            budget=budget(),
        )
        save(record, report)
        assert state["status"] == "completed", "native_failed_no_replay"

    try:
        await run_case(
            "record",
            "admin",
            3,
            "请记住：以后请在我的答复开头使用‘蓝鹭验收：’，这是文字风格偏好。现在请用一句中文打个招呼。",
        )
        recalled = {}
        for _ in range(36):
            await memory.drain()
            recalled = await memory.context(owner)
            if "蓝鹭" in recalled.get("context", ""):
                break
            await asyncio.sleep(5)
        save(OUT / "private/memory-recalled.json", recalled)
        report["honcho_recalled_preference"] = "蓝鹭" in recalled.get("context", "")
        save(record, report)
        await run_case(
            "fresh_adoption", "admin", 4, "请用一句中文向我问好。", expected="蓝鹭验收："
        )
        memory.correct(owner, "以后请改用‘晨舟验收：’作为答复开头，停用‘蓝鹭验收：’。", uuid4())
        await run_case(
            "correction",
            "admin",
            5,
            "请用一句中文向我问好。",
            expected="晨舟验收：",
            forbidden="蓝鹭验收：",
        )
        await run_case("other_owner", "ZYJ", 5, "请用一句中文向我问好。", forbidden="验收：")
        memory.forget(owner)
        for _ in range(24):
            await memory.drain()
            if memory.status(owner)["deletion"] == "completed" and not await memory.client.exists(
                workspace
            ):
                break
            await asyncio.sleep(2)
        report["deletion"] = {
            "status": memory.status(owner)["deletion"],
            "old_workspace_absent": not await memory.client.exists(workspace),
        }
        await run_case("after_delete", "admin", 2, "请用一句中文向我问好。", forbidden="验收：")
        report["status"] = (
            "passed"
            if report["honcho_recalled_preference"]
            and all(c["status"] == "passed" for c in report["cases"])
            and report["deletion"]["old_workspace_absent"]
            else "gaps_found"
        )
    except Exception as exc:
        report["status"] = "stopped_no_replay"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        for cleanup_owner in [owner, "scat-zyj"]:
            if memory.status(cleanup_owner)["enabled"]:
                memory.forget(cleanup_owner)
        await memory.drain()
        report["cleanup"] = {o: memory.status(o) for o in [owner, "scat-zyj"]}
        report["budget"] = budget()
        save(record, report)
        await runtime.aclose()
        await memory.aclose()
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
