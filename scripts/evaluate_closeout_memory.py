"""One hard-budgeted, synthetic-owner memory API acceptance on the real Honcho service."""

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
OUT = ROOT / "var/validation/20261108-platform-closeout"
sys.path.insert(0, str(ROOT / "src"))


async def main():
    import httpx
    from fastapi import FastAPI

    from hermes_runtime.autonomy_config import AutonomyPolicy
    from hermes_runtime.honcho_memory import MemoryRuntime
    from service.auth import verify_bearer
    from service.hermes_api import get_hermes_owner
    from service.memory_api import router

    config = json.loads((ROOT / "var/deploy/autonomy/campaign-closeout.json").read_text())
    assert time.time() < config["expires_at"]
    async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
        assert (await http.get("http://127.0.0.1:18142/health")).status_code == 200
    marker = OUT / "memory-admitted.json"
    with marker.open("x") as f:
        json.dump(
            {
                "campaign": config["id"],
                "max_requests": config["max_requests"],
                "max_units": config["max_units"],
                "synthetic_owners": 2,
            },
            f,
        )
    private = OUT / "private-memory"
    private.mkdir(exist_ok=False)
    private.chmod(0o700)
    policy = AutonomyPolicy.model_validate(
        json.loads((ROOT / "var/deploy/autonomy/config.json").read_text())
    ).memory
    policy = policy.model_copy(update={"state": str((private / "state.sqlite").relative_to(ROOT))})
    memory = MemoryRuntime(ROOT, policy)
    owners = ["closeout-memory-" + uuid4().hex + suffix for suffix in ["-a", "-b"]]
    (private / "owners.json").write_text(json.dumps(owners))
    (private / "owners.json").chmod(0o600)
    owner = owners[0]
    app = FastAPI()
    app.include_router(router)
    app.state.memory_runtime = memory
    app.dependency_overrides[verify_bearer] = lambda: None
    app.dependency_overrides[get_hermes_owner] = lambda: owner
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acceptance")
    budget_path = ROOT / "var/autonomy/budget/state.sqlite"

    def usage():
        with sqlite3.connect("file:" + str(budget_path) + "?mode=ro", uri=True) as db:
            base = db.execute(
                "SELECT base_id FROM model_campaigns WHERE id=?", (config["id"],)
            ).fetchone()[0]
            n, units = db.execute(
                "SELECT count(*),coalesce(sum(reserved),0) FROM model_calls WHERE id>?", (base,)
            ).fetchone()
            return {"requests": n, "reserved_units": units}

    report = {
        "status": "running",
        "checks": {},
        "real_honcho": True,
        "native_agent_adoption_evaluated": False,
        "http_auth": "in_process_trusted_synthetic_owner_override",
        "campaign": config,
    }

    def save():
        (OUT / "memory-acceptance.json").write_text(json.dumps(report, indent=2) + "\n")

    async def context(which):
        nonlocal owner
        owner = which
        response = await client.get("/api/v1/hermes/memory/context")
        response.raise_for_status()
        return response.json().get("context", "")

    async def wait_for(which, pattern):
        deadline = time.monotonic() + 150
        while True:
            spent = usage()
            assert (
                spent["requests"] <= config["max_requests"]
                and spent["reserved_units"] <= config["max_units"]
            )
            text = await context(which)
            if re.search(pattern, text, re.IGNORECASE):
                return True
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(4)

    async def correct(which, text):
        nonlocal owner
        owner = which
        body = {"request_id": str(uuid4()), "content": text}
        first = await client.post("/api/v1/hermes/memory/corrections", json=body)
        assert first.status_code == 202 and first.json()["replayed"] is False
        repeated = await client.post("/api/v1/hermes/memory/corrections", json=body)
        assert repeated.status_code == 202 and repeated.json()["replayed"] is True
        await memory.drain()
        records = (await client.get("/api/v1/hermes/memory/corrections")).json()["items"]
        assert sum(r["request_id"] == body["request_id"] for r in records) == 1

    try:
        report["checks"]["b_initial_empty"] = (await context(owners[1])) in {
            "",
            '{"representation": null, "peer_card": null}',
        }
        assert report["checks"]["b_initial_empty"]
        await correct(owners[0], "请记住我的距离单位偏好是公里（kilometres）。")
        report["checks"]["a_recall_kilometres"] = await wait_for(owners[0], r"公里|kilomet|\bkm\b")
        save()
        assert report["checks"]["a_recall_kilometres"]
        await correct(owners[1], "请记住我的距离单位偏好是海里（nautical miles）。")
        report["checks"]["b_recall_nautical_miles"] = await wait_for(owners[1], r"海里|nautical")
        save()
        assert report["checks"]["b_recall_nautical_miles"]
        report["checks"]["a_not_contaminated_by_b"] = not bool(
            re.search(r"海里|nautical", await context(owners[0]), re.IGNORECASE)
        )
        await correct(owners[0], "请更正距离单位偏好：今后使用米（metres），之前的公里偏好已作废。")
        report["checks"]["correction_visible"] = await wait_for(
            owners[0], r"(?<!公)米|\bmet(?:er|re)s?\b"
        )
        save()
        assert report["checks"]["correction_visible"]
        await memory.aclose()
        memory = MemoryRuntime(ROOT, policy)
        app.state.memory_runtime = memory
        report["checks"]["adapter_reopen_preserves_recall"] = bool(
            re.search(r"(?<!公)米|\bmet(?:er|re)s?\b", await context(owners[0]), re.IGNORECASE)
        )
        report["correction_measure"] = (
            "expected unit visible in real Honcho summary; historical negative mentions and general semantic accuracy require separate review"
        )
    except (AssertionError, ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        report["error_type"] = type(exc).__name__
    finally:
        report["cleanup"] = {}
        for i, which in enumerate(owners):
            owner = which
            try:
                response = await client.delete("/api/v1/hermes/memory")
                assert response.status_code == 202 and response.json()["deletion"] == "pending"
                deadline = time.monotonic() + 120
                while True:
                    await memory.drain()
                    state = (await client.get("/api/v1/hermes/memory")).json()
                    if state["deletion"] == "completed" or time.monotonic() > deadline:
                        break
                    await asyncio.sleep(3)
                records = (await client.get("/api/v1/hermes/memory/corrections")).json()
                report["cleanup"][str(i)] = {
                    "deletion": state["deletion"],
                    "enabled": state["enabled"],
                    "corrections_empty": not records["items"],
                }
            except (AssertionError, ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
                report["cleanup"][str(i)] = {"error_type": type(exc).__name__}
        await client.aclose()
        await memory.aclose()
        report["usage"] = usage()
        report["status"] = (
            "passed"
            if not report.get("error_type")
            and all(report["checks"].values())
            and all(
                x.get("deletion") == "completed"
                and x.get("enabled") is False
                and x.get("corrections_empty")
                for x in report["cleanup"].values()
            )
            else "failed_or_incomplete"
        )
        save()
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(main())
