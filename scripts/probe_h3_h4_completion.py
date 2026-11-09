"""H3/H4 real scoped-result and cancellation integration, using owned test services."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
HERMES = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"
TERMINAL = {"completed", "cancelled", "failed", "interrupted"}


async def collect(client, path):
    events = []
    async with client.stream("GET", path + "/events", timeout=180) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                value = json.loads(line[6:])
                events.append(value)
                if value.get("event", "").removeprefix("run.") in TERMINAL:
                    return events
                if value.get("event") == "stream.error":
                    raise RuntimeError("stream_error:" + value["code"])
    raise RuntimeError("stream_without_terminal")


async def exercise(report, save):
    identity = uuid4().hex[:12]
    temporary = ROOT / ".tmp/h34" / ("probe-" + identity)
    temporary.mkdir(parents=True, exist_ok=False)
    profile = temporary / "runtime"
    report["profile"] = str(profile.relative_to(ROOT))
    for port in [18084, 18085]:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("probe_port_in_use")
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "HERMES_BASE_URL": "http://127.0.0.1:18085",
        "HERMES_HOME": str(profile),
        "HERMES_STATE_FILE": str(temporary / "bindings.sqlite"),
        "HERMES_RESULT_STATE_FILE": str(temporary / "results.sqlite"),
    }
    children = []
    logs = []
    commands = [
        [
            HERMES,
            "-B",
            str(ROOT / "scripts/run_hermes_api.py"),
            "--with-mcp",
            "--home",
            str(profile.relative_to(ROOT)),
            "--port",
            "18085",
        ],
        [PYTHON, "-B", str(ROOT / "scripts/run_platform_api.py"), "--port", "18084"],
    ]
    try:
        for i, command in enumerate(commands):
            log_path = ROOT / "var/validation" / f"h34-probe-{identity}-{i}.log"
            log = log_path.open("w")
            log_path.chmod(0o600)
            logs.append(log)
            child = await asyncio.create_subprocess_exec(
                *command, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
            )
            children.append(child)
            url = (
                "http://127.0.0.1:18085/health" if i == 0 else "http://127.0.0.1:18084/health/ready"
            )
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                for _ in range(120):
                    if child.returncode is not None:
                        raise RuntimeError("probe_child_exited")
                    try:
                        if (await client.get(url)).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("probe_start_timeout")
        values = dotenv_values(ROOT / ".env")
        headers = (
            {"Authorization": "Bearer " + values["AUTH_SECRET"]}
            if values.get("AUTH_SECRET")
            else {}
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18084", headers=headers, trust_env=False, timeout=180
        ) as client:
            tid = (await client.post("/api/v1/hermes/threads", json={})).json()["thread_id"]
            base = f"/api/v1/hermes/threads/{tid}/runs"

            async def question(message, operation):
                request = {"request_id": str(uuid4()), "message": message}
                created = await client.post(base, json=request)
                created.raise_for_status()
                rid = created.json()["run_id"]
                path = base + "/" + rid
                frames = await collect(client, path)
                state = (await client.get(path)).json()
                assert state["status"] == "completed" and state.get("completed") is not False
                refs = (await client.get(path + "/results")).json()["items"]
                assert refs and any(ref["operation"] == operation for ref in refs), (
                    "missing_real_result_reference"
                )
                chosen = next(ref for ref in refs if ref["operation"] == operation)
                receipt = (await client.get(path + "/results/" + chosen["result_id"])).json()
                body = json.dumps(
                    receipt["result"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
                assert hashlib.sha256(body).hexdigest() == chosen["payload_sha256"]
                assert any(
                    event.get("event") == "business.result"
                    and event.get("result_id") == chosen["result_id"]
                    for event in frames
                )
                assert state["result_references"] == refs
                replay = (await client.post(base, json=request)).json()
                assert replay["run_id"] == rid and replay["replayed"] is True
                return path, chosen, receipt, frames

            report["stage"] = "search"
            save()
            path, ref, receipt, frames = await question(
                "检索 scat20161015_20161021 批次的航班，只返回第一条摘要（limit=1），说明总匹配数。",
                "search_scat_flights",
            )
            first = receipt["result"]["data"]["items"][0]
            external = str(first["source"]["external_id"])
            report["search"] = {
                "reference": ref,
                "total": receipt["result"]["data"]["total"],
                "count": receipt["result"]["data"]["count"],
                "business_result_events": sum(x.get("event") == "business.result" for x in frames),
            }
            # Independent deterministic REST uses the same data, without any LLM arithmetic.
            rest = await client.get(
                "/api/v1/scat/flights", params={"batch": "scat20161015_20161021", "limit": 1}
            )
            rest.raise_for_status()
            assert rest.json()["pagination"]["total"] == report["search"]["total"]
            assert report["search"]["count"] == 1
            report["rest_result_parity"] = True
            save()
            other = (await client.post("/api/v1/hermes/threads", json={})).json()["thread_id"]
            foreign = await client.get(path.replace(tid, other) + "/results/" + ref["result_id"])
            assert foreign.status_code == 404
            assert (
                await client.post(
                    base,
                    json={
                        "request_id": str(uuid4()),
                        "message": "hello",
                        "user_id": "forged-owner",
                    },
                )
            ).status_code == 422
            report["foreign_thread_denied"] = True
            save()
            report["stage"] = "track"
            save()
            _track_path, track_ref, track_receipt, track_frames = await question(
                f"查询 scat20161015_20161021 批次航班 {external} 的前3个轨迹点，start=0，limit=3，fields仅position，报告返回点数。",
                "get_scat_track_points",
            )
            data = track_receipt["result"]["data"]
            assert data["pagination"]["returned_points"] == 3 and len(data["points"]) == 3
            report["track"] = {
                "reference": track_ref,
                "returned_points": 3,
                "business_result_events": sum(
                    x.get("event") == "business.result" for x in track_frames
                ),
            }
            save()
            report["stage"] = "cancel"
            save()
            created = await client.post(
                base,
                json={
                    "request_id": str(uuid4()),
                    "message": "检索知识库详细解释 ADE 和 FDE，给出文档出处并详细展开。",
                },
            )
            created.raise_for_status()
            cancel_path = base + "/" + created.json()["run_id"]
            stop = await client.post(cancel_path + "/stop", json={})
            stop.raise_for_status()
            stopped = stop.json()
            assert (
                stopped["cancellation_scope"] == "conversation_only"
                and stopped["business_tasks_cancelled"] is False
            )
            for _ in range(60):
                stopped = (await client.get(cancel_path)).json()
                if stopped["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.5)
            assert (
                stopped["status"] in {"cancelled", "interrupted"}
                and stopped.get("completed") is not True
            )
            report["cancel"] = {
                "status": stopped["status"],
                "scope": "conversation_only",
                "business_jobs_submitted": 0,
            }
            save()
            os.killpg(children[0].pid, signal.SIGTERM)
            await asyncio.wait_for(children[0].wait(), 20)
            saved = await client.get(path + "/results/" + ref["result_id"])
            assert saved.status_code == 200
            live = await client.get(
                "/api/v1/scat/flights", params={"batch": "scat20161015_20161021", "limit": 1}
            )
            assert live.status_code == 200
            ready = await client.get("/health/ready")
            assert ready.status_code == 503
            report["native_offline_receipt_read"] = True
            report["deterministic_api_survives"] = True
            report["stage"] = "done"
            report["status"] = "passed"
            save()
    finally:
        for child in reversed(children):
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(child.wait(), 15)
                except TimeoutError:
                    os.killpg(child.pid, signal.SIGKILL)
                    await child.wait()
        for log in logs:
            log.close()
        resolved = temporary.resolve()
        if (
            resolved.is_relative_to((ROOT / ".tmp/h34").resolve())
            and resolved.name == "probe-" + identity
        ):
            shutil.rmtree(resolved)
        report["cleanup"] = {
            "owned_processes_stopped": all(c.returncode is not None for c in children),
            "owned_directory_removed": not temporary.exists(),
        }
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="h34-completion-attempt1.json")
    args = parser.parse_args()
    if Path(args.report).name != args.report:
        raise ValueError("report must be a filename")
    report_path = ROOT / "var/validation" / args.report
    if report_path.exists():
        raise FileExistsError("Preserve prior reports; choose a new report name")
    report = {
        "status": "running",
        "stage": "starting",
        "started_at": time.time(),
        "implementation_sha256": {
            n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
            for n in [
                "scripts/run_hermes_api.py",
                "src/hermes_runtime/results.py",
                "src/application/readonly.py",
                "src/service/hermes_api.py",
                "src/scat_mcp/server.py",
                "src/hermes_runtime/client.py",
                "src/service/hermes_app.py",
                "src/core/settings.py",
            ]
        },
    }

    def save():
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        report_path.chmod(0o600)

    save()
    try:
        asyncio.run(exercise(report, save))
    except Exception as exc:  # noqa: BLE001 -- preserve diagnostics and owned-process cleanup.
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        report["failure"] = (
            str(exc) if isinstance(exc, (AssertionError, RuntimeError)) else type(exc).__name__
        )
    finally:
        report["finished_at"] = time.time()
        save()
    print(
        json.dumps(
            {"status": report["status"], "stage": report["stage"], "cleanup": report.get("cleanup")}
        )
    )
    raise SystemExit(0 if report["status"] == "passed" else 2)


if __name__ == "__main__":
    main()
