"""Real Honcho smoke with isolated synthetic owners; no native agent quality claim."""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing_Anaconda_required")
    # Avoid creating any observations when the model exit cannot be reached.
    import httpx

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(8, connect=2)) as http:
            await http.get("https://api.deepseek.com")
    except httpx.HTTPError:
        report = {
            "status": "blocked_model_network_preflight",
            "observations_written": 0,
            "model_calls_started": 0,
        }
        (ROOT / "var/validation/20261107-memory-settings-ui/real-honcho-preflight.json").write_text(
            json.dumps(report, indent=2)
        )
        print(json.dumps(report), flush=True)
        return
    from hermes_runtime.autonomy_config import AutonomyPolicy
    from hermes_runtime.honcho_memory import MemoryRuntime

    policy = AutonomyPolicy.model_validate(
        json.loads((ROOT / "var/deploy/autonomy/config.json").read_text())
    ).memory
    policy = policy.model_copy(update={"state": ".tmp/memory-settings-ui/real/state.sqlite"})
    budget = ROOT / "var/autonomy/budget/state.sqlite"

    def usage():
        with sqlite3.connect("file:" + budget.as_posix() + "?mode=ro", uri=True) as db:
            return list(
                db.execute("SELECT count(*),coalesce(sum(reserved),0) FROM model_calls").fetchone()
            )

    before = usage()
    assert before[0] + 12 <= 240 and before[1] + 500000 <= 8000000, "insufficient_existing_budget"
    memory = MemoryRuntime(ROOT, policy)
    label = "memory-ui-synthetic-" + uuid4().hex
    owners = [label + "-a", label + "-b"]
    report = {
        "evaluation": "real_honcho_synthetic_storage_smoke",
        "native_agent_quality_evaluated": False,
        "before_usage": before,
        "checks": {},
        "stop_thresholds": {"additional_requests": 12, "additional_reserved_units": 500000},
        "async_inflight_may_overshoot_threshold": True,
        "isolated_owners_planned": 2,
    }

    async def wait_context(owner, pattern):
        deadline = time.monotonic() + 90
        while True:
            used = usage()
            if used[0] - before[0] > 12 or used[1] - before[1] > 500000:
                raise ValueError("evaluation_budget_exceeded")
            value = await memory.context(owner)
            if value.get("available") and re.search(pattern, value["context"], re.IGNORECASE):
                return True
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(5)

    try:
        memory.correct(owners[0], "请记住：我偏好的距离单位是公里。", uuid4())
        await memory.drain()
        report["checks"]["a_recall_kilometres"] = await wait_context(
            owners[0], r"公里|kilomet|\bkm\b"
        )
        report["checks"]["b_initial_empty"] = (await memory.context(owners[1])).get(
            "context", ""
        ) in {"", '{"representation": null, "peer_card": null}'}
        memory.correct(owners[1], "请记住：我偏好的距离单位是海里。", uuid4())
        await memory.drain()
        report["checks"]["b_recall_nautical_miles"] = await wait_context(
            owners[1], r"海里|nautical"
        )
        a_context = await memory.context(owners[0])
        report["checks"]["a_not_contaminated_by_b"] = not bool(
            re.search(r"海里|nautical", a_context.get("context", ""), re.IGNORECASE)
        )
        memory.correct(owners[0], "请更正距离单位偏好：今后使用米，之前的公里偏好已作废。", uuid4())
        await memory.drain()
        report["checks"]["correction_visible"] = await wait_context(
            owners[0], r"(?<!公)米|\bmet(?:er|re)s?\b"
        )
        await memory.aclose()
        memory = MemoryRuntime(ROOT, policy)
        report["checks"]["restart_readable"] = bool(
            re.search(
                r"(?<!公)米|\bmet(?:er|re)s?\b",
                (await memory.context(owners[0])).get("context", ""),
                re.IGNORECASE,
            )
        )
        report["correction_effect_measure"] = (
            "expected unit keyword visible; conflicting historical statements and native-agent adoption are not semantically judged"
        )
    except (ValueError, OSError, RuntimeError) as exc:
        report["error"] = type(exc).__name__
    finally:
        report["cleanup"] = {}
        for index, owner in enumerate(owners):
            memory.forget(owner)
            for _ in range(6):
                await memory.drain()
                if memory.status(owner)["deletion"] == "completed":
                    break
                await asyncio.sleep(5)
            report["cleanup"][str(index)] = {
                "deletion": memory.status(owner)["deletion"],
                "enabled": memory.status(owner)["enabled"],
            }
        await memory.aclose()
        report["after_usage"] = usage()
        report["usage_delta"] = [a - b for a, b in zip(report["after_usage"], before)]
        report["status"] = (
            "passed"
            if not report.get("error")
            and all(report["checks"].values())
            and all(
                v["deletion"] == "completed" and not v["enabled"]
                for v in report["cleanup"].values()
            )
            else "incomplete_or_failed"
        )
        out = ROOT / "var/validation/20261107-memory-settings-ui/real-honcho.json"
        out.write_text(json.dumps(report, indent=2) + "\n")
        out.chmod(0o600)
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(main())
