"""Real FastAPI/Hermes/MCP smoke with bounded requests and owned process cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "var/validation/h4-platform-api.json"
PYTHON = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
HERMES_PYTHON = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def save(report):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    REPORT.chmod(0o600)


async def events(client, path, *, cursor=None, disconnect=False):
    headers = {"Last-Event-ID": str(cursor)} if cursor is not None else {}
    received = []
    async with client.stream("GET", path, headers=headers, timeout=120) as response:
        response.raise_for_status()
        assert response.headers.get("x-accel-buffering") == "no"
        sequence = None
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                sequence = int(line[4:])
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            received.append((sequence, event))
            if disconnect or event.get("event") in {
                "run.completed",
                "run.failed",
                "run.cancelled",
                "run.interrupted",
                "stream.error",
            }:
                break
    return received


async def run(report):
    identity = uuid4().hex[:12]
    profile = ROOT / "var/hermes" / ("probe-business-api-" + identity)
    temporary = ROOT / ".tmp/h4/platform-probe"
    temporary.mkdir(parents=True, exist_ok=True)
    platform_state = temporary / ("bindings-" + identity + ".sqlite")
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_HOME": str(profile),
        "HERMES_STATE_FILE": str(platform_state),
    }
    report.update(
        profile=str(profile.relative_to(ROOT)), platform_state=str(platform_state.relative_to(ROOT))
    )
    children = []
    logs = []
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        for port in [18080, 18081]:
            try:
                await client.get(f"http://127.0.0.1:{port}/health/live")
            except httpx.TransportError:
                pass
            else:
                raise RuntimeError("probe_port_in_use")
    commands = [
        [
            HERMES_PYTHON,
            "-B",
            str(ROOT / "scripts/run_hermes_api.py"),
            "--with-mcp",
            "--home",
            str(profile.relative_to(ROOT)),
            "--port",
            "18081",
        ],
        [PYTHON, "-B", str(ROOT / "scripts/run_platform_api.py"), "--port", "18080"],
    ]
    try:
        for index, command in enumerate(commands):
            log_path = ROOT / "var/validation" / f"h4-process-{index}-{identity}.log"
            log = log_path.open("w")
            log_path.chmod(0o600)
            logs.append(log)
            child = await asyncio.create_subprocess_exec(
                *command, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
            )
            children.append(child)
            health = (
                "http://127.0.0.1:18081/health"
                if index == 0
                else "http://127.0.0.1:18080/health/ready"
            )
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                for _ in range(90):
                    if child.returncode is not None:
                        raise RuntimeError("probe_child_exited")
                    try:
                        response = await client.get(health)
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("probe_health_timeout")
        report["health"] = "ready"
        report["stage"] = "business"
        save(report)
        from dotenv import dotenv_values

        values = dotenv_values(ROOT / ".env")
        headers = (
            {"Authorization": "Bearer " + values["AUTH_SECRET"]}
            if values.get("AUTH_SECRET")
            else {}
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18080", trust_env=False, timeout=120, headers=headers
        ) as client:
            response = await client.post("/api/v1/hermes/threads", json={})
            response.raise_for_status()
            tid = response.json()["thread_id"]
            base = f"/api/v1/hermes/threads/{tid}/runs"
            nonce = "SCAT-" + uuid4().hex[:8].upper()
            body = {
                "request_id": str(uuid4()),
                "message": f"记住本次校验代号 {nonce}。请仅调用 search_scat_flights 一次，"
                "查询批次 scat20161015_20161021，limit=1。只回答该批次 total 总航班数和校验代号。",
            }
            response = await client.post(base, json=body)
            response.raise_for_status()
            rid = response.json()["run_id"]
            replay = await client.post(base, json=body)
            replay.raise_for_status()
            report["idempotency_replayed_same_run"] = (
                replay.json()["run_id"] == rid and replay.json().get("replayed") is True
            )
            path = base + "/" + rid + "/events"
            first = await events(client, path, disconnect=True)
            assert first and first[-1][0] is not None
            rest = await events(client, path, cursor=first[-1][0])
            combined = first + rest
            names = Counter(event["event"] for _, event in combined)
            terminal = combined[-1][1]
            output = terminal.get("output", "")
            tools = [
                event.get("tool") for _, event in combined if event["event"] == "tool.completed"
            ]
            reference = json.loads((ROOT / "var/validation/h3-real-business.json").read_text())[
                "mongodb"
            ]["batch_count"]
            report["business_run"] = {
                "terminal": terminal["event"],
                "events": dict(names),
                "tool": tools,
                "count_matches": str(reference) in output.replace(",", ""),
                "nonce_present": nonce in output,
                "reconnect_ids_increase": all(
                    sequence is not None and sequence > first[-1][0] for sequence, _ in rest
                ),
            }
            assert terminal["event"] == "run.completed" and report["business_run"]["count_matches"]
            assert "mcp__scat__search_scat_flights" in tools
            assert (
                report["idempotency_replayed_same_run"]
                and report["business_run"]["reconnect_ids_increase"]
            )
            save(report)
            response = await client.post(
                base,
                json={
                    "request_id": str(uuid4()),
                    "message": "不调用工具，仅原样回复上一轮的校验代号。",
                },
            )
            response.raise_for_status()
            rid = response.json()["run_id"]
            result = await events(client, base + "/" + rid + "/events")
            report["multi_turn_memory"] = (
                result[-1][1].get("output", "").strip().strip("。.") == nonce
            )
            assert report["multi_turn_memory"]
            response = await client.post(
                base,
                json={
                    "request_id": str(uuid4()),
                    "message": "请调用 search_scat_knowledge 查询 ADE 和 FDE 的含义与单位，limit=2。简短回答并引用返回的 source_id。",
                },
            )
            response.raise_for_status()
            rid = response.json()["run_id"]
            result = await events(client, base + "/" + rid + "/events")
            source_ids = json.loads((ROOT / "var/validation/h3-real-business.json").read_text())[
                "operations"
            ]["search_scat_knowledge"]["source_ids"]
            report["rag_run"] = {
                "terminal": result[-1][1]["event"],
                "tool_called": any(
                    event.get("tool") == "mcp__scat__search_scat_knowledge" for _, event in result
                ),
                "source_cited": any(
                    source in result[-1][1].get("output", "") for source in source_ids
                ),
            }
            assert (
                report["rag_run"]["terminal"] == "run.completed"
                and report["rag_run"]["tool_called"]
                and report["rag_run"]["source_cited"]
            )
            save(report)
            response = await client.post(
                base,
                json={
                    "request_id": str(uuid4()),
                    "message": "这是一项取消协议测试。请写一篇至少2000字的航空轨迹质量说明，无需调用工具。",
                },
            )
            response.raise_for_status()
            rid = response.json()["run_id"]
            stopped = await client.post(base + "/" + rid + "/stop", json={})
            stopped.raise_for_status()
            for _ in range(100):
                response = await client.get(base + "/" + rid)
                response.raise_for_status()
                state = response.json()
                if state["status"] in {"cancelled", "interrupted", "completed", "failed"}:
                    break
                await asyncio.sleep(0.2)
            report["cancellation"] = {
                "actual_status": state["status"],
                "completed": state.get("completed"),
            }
            assert state["status"] in {"cancelled", "interrupted"}
            report["stage"] = "degradation"
            save(report)
            children[0].terminate()
            await asyncio.wait_for(children[0].wait(), timeout=20)
            readiness = await client.get("/health/ready")
            deterministic = await client.get(
                "/api/v1/scat/flights", params={"batch": "scat20161015_20161021", "limit": 1}
            )
            report["degradation"] = {
                "readiness_status": readiness.status_code,
                "deterministic_http_status": deterministic.status_code,
                "database_available": readiness.json().get("checks", {}).get("mongodb"),
            }
            assert readiness.status_code == 503 and deterministic.status_code == 200
            assert report["degradation"]["database_available"]
            report["status"] = "passed"
            report["stage"] = "completed"
    finally:
        for child in reversed(children):
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), timeout=20)
                except TimeoutError:
                    # Each group was created by this driver, with its PID as group ID.
                    if os.getpgid(child.pid) == child.pid:
                        os.killpg(child.pid, signal.SIGKILL)
                    await child.wait()
        for log in logs:
            log.close()
        report["child_pids"] = [child.pid for child in children]
        report["children_stopped"] = all(child.returncode is not None for child in children)


if __name__ == "__main__":
    report = {"status": "running", "started_at": time.time()}
    save(report)
    try:
        asyncio.run(run(report))
    except Exception as exc:  # noqa: BLE001 -- only a safe type is emitted.
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, RuntimeError) and str(exc) in {
            "probe_port_in_use",
            "probe_child_exited",
            "probe_health_timeout",
        }:
            report["error_code"] = str(exc)
    report["finished_at"] = time.time()
    save(report)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)
