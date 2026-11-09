"""Finite synthetic S5 planner/approval/background/report probe with isolated state."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
HERMES = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


async def stop(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            await asyncio.to_thread(child.wait, timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)


async def consume(client, path):
    last = None
    async with client.stream("GET", path, timeout=240) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            last = json.loads(line[6:])
            if last.get("event") in {
                "run.completed",
                "run.failed",
                "run.cancelled",
                "run.interrupted",
                "stream.error",
            }:
                break
    assert last and last["event"] == "run.completed", "native_planning_did_not_complete"


async def probe(report):
    from dotenv import dotenv_values
    from pymongo import AsyncMongoClient

    from application.skill_cpu_backend import process_start, request_stop
    from application.skill_job_factory import create_skill_tasks

    for port in (18096, 18097, 18098):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    identity = uuid4().hex[:10]
    home = ROOT / ".tmp/s5" / ("native-" + identity)
    home.mkdir(parents=True)
    values = dotenv_values(ROOT / ".env")
    collections = ["analysis_runs_s5_" + identity, "analysis_artifacts_s5_" + identity]
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(home),
        "TMP": str(home),
        "TEMP": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_BASE_URL": "http://127.0.0.1:18097",
        "HERMES_STATE_FILE": str(home / "platform.sqlite"),
        "HERMES_TASK_STATE_FILE": str(home / "jobs.sqlite"),
        "HERMES_RESULT_STATE_FILE": str(home / "results.sqlite"),
        "HERMES_SKILL_JOBS_ENABLED": "true",
        "HERMES_WORKFLOWS_ENABLED": "true",
        "SCAT_S5_WORKFLOWS": "1",
        "SCAT_S5_SYNTHETIC": "1",
        "ANALYSIS_IDENTITY_MODE": "single_user",
        "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
        "ANALYSIS_RUNS_COLLECTION": collections[0],
        "ANALYSIS_ARTIFACTS_COLLECTION": collections[1],
        "SCAT_GBAR_V23_ENABLED": "false",
        "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
        "SCAT_JOB_TEMP_DIR": str(home / "worker-tmp"),
    }
    children = []
    report.update(
        probe_id=identity, stage="startup", python=APP, native_python=HERMES, synthetic=True
    )

    async def launch(command, name, health=None):
        log_path = ROOT / "var/validation" / ("s5-native-" + identity + "-" + name + ".log")
        with log_path.open("x", encoding="utf-8") as log:
            log_path.chmod(0o600)
            child = await asyncio.to_thread(
                subprocess.Popen,
                command,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        children.append(child)
        if health:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
                for _ in range(100):
                    if child.poll() is not None:
                        raise RuntimeError(name + "_exited")
                    try:
                        if (await http.get(health)).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError(name + "_unavailable")
        return child

    try:
        task = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        await launch(
            [
                HERMES,
                "-B",
                str(ROOT / "scripts/run_hermes_api.py"),
                "--home",
                str((home / "main").relative_to(ROOT)),
                "--port",
                "18097",
                "--with-mcp",
                "--synthetic-mcp",
                "--with-model-tasks",
                "--with-skills",
                "--with-workflows",
            ],
            "main",
            "http://127.0.0.1:18097/health",
        )
        await launch(
            [APP, "-B", str(ROOT / "scripts/run_platform_api.py"), "--port", "18096"],
            "api",
            "http://127.0.0.1:18096/health/live",
        )
        headers = (
            {"Authorization": "Bearer " + values["AUTH_SECRET"]}
            if values.get("AUTH_SECRET")
            else {}
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18096", headers=headers, trust_env=False, timeout=240
        ) as client:
            catalog = await client.get("/api/v1/hermes/workflow-catalog")
            catalog.raise_for_status()
            assert catalog.json()["enabled"] and catalog.json()["synthetic"]
            thread = await client.post("/api/v1/hermes/threads", json={})
            thread.raise_for_status()
            tid = thread.json()["thread_id"]
            base = f"/api/v1/hermes/threads/{tid}"
            response = await client.post(
                base + "/runs",
                json={
                    "request_id": str(uuid4()),
                    "message": '这是合成数据验收。先 skills_list，加载 scat-model-comparison 正文并查询 scat_workflow_catalog，然后仅准备一次以下比较方案：kind=comparison，batch=SYNTH-S5，external_ids=[SYNTH-S5-001,SYNTH-S5-002]，models=[{"model":"constant-velocity-heading"},{"model":"kalman-cv"}]，materializations=["phase_segments"]，missing_phase=build，title=合成双模型对比。其他参数用默认值。只准备并给出页面确认提示，不执行计算，不查询真实业务数据，不使用旧 scat_task_prepare。',
                },
            )
            response.raise_for_status()
            parent = response.json()["run_id"]
            report.update(stage="planner", parent_run_id=parent)
            await consume(client, base + "/runs/" + parent + "/events")
            listed = await client.get(base + "/model-tasks")
            listed.raise_for_status()
            items = listed.json()["items"]
            assert len(items) == 1 and items[0]["confirmation_required"] and "workflow" in items[0]
            view = items[0]
            task_id = view["task_id"]
            endpoint = base + "/model-tasks/" + task_id
            report.update(stage="background", task_id=task_id)
            with sqlite3.connect(home / "jobs.sqlite") as db:
                assert (
                    db.execute("SELECT count(*) FROM skill_backend_executions").fetchone()[0] == 0
                )
            approval = {"snapshot_sha256": view["snapshot_sha256"], "confirmed": True}
            for _ in range(2):
                result = await client.post(endpoint + "/confirm", json=approval)
                result.raise_for_status()
            worker = await launch(
                [
                    APP,
                    "-B",
                    str(ROOT / "scripts/run_model_worker.py"),
                    "--once",
                    "--job-home",
                    str((home / "background").relative_to(ROOT)),
                    "--background-port",
                    "18098",
                ],
                "worker",
            )
            deadline = time.monotonic() + 480
            while worker.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(1)
            assert worker.poll() == 0, "background_worker_did_not_finish"
            response = await client.get(endpoint)
            response.raise_for_status()
            final = response.json()
            assert final["skill_job"]["state"] == final["run"]["status"] == "succeeded", (
                "workflow_result_not_verified"
            )
            backends = final["skill_job"]["backend_executions"]
            assert len(backends) == 3 and all(
                b["calls"] == 1 and b["state"] == "succeeded" for b in backends
            )
            result_id = final["run"]["result_artifact_ids"][0]
            artifact = await client.get(endpoint + "/artifacts/" + result_id)
            artifact.raise_for_status()
            analysis = artifact.json()["analysis_result"]
            assert analysis["schema_version"] == "1.4" and analysis["context"]["synthetic"]
            assert any(b.get("title") == "模型与共同样本对比" for b in analysis["blocks"])
            other = await client.post("/api/v1/hermes/threads", json={})
            other.raise_for_status()
            assert (
                await client.get(endpoint.replace(tid, other.json()["thread_id"]))
            ).status_code == 404
            traces = []
            for path in [
                home / "main/skill-loads.sqlite",
                *home.glob("background/*/*/skill-loads.sqlite"),
            ]:
                with sqlite3.connect(path) as db:
                    traces.extend(
                        {"skill": r[0], "operation": r[1], "outcome": r[2]}
                        for r in db.execute(
                            "SELECT skill_id,operation,outcome FROM skill_operations"
                        )
                    )
            assert {
                "scat-model-comparison",
                "scat-data-materialization",
                "scat-evaluation",
                "scat-report",
            } <= {t["skill"] for t in traces}
            assert all(t["outcome"] == "returned" for t in traces)
            report.update(
                status="passed",
                stage="verified",
                backend_executions=backends,
                groups=final["workflow"]["tool_groups"],
                result_artifact_id=result_id,
                result_payload_sha256=artifact.json()["payload_sha256"],
                analysis_result=analysis,
                budget=final["skill_job"]["budget"],
                admissions=traces,
                confirmation_replay_single_job=True,
                scope_isolation=True,
            )
    finally:
        for child in reversed(children):
            await stop(child)
        inputs = []
        if (home / "jobs.sqlite").exists():
            app = create_skill_tasks(
                ROOT, home / "jobs.sqlite", workflows_enabled=True, workflow_synthetic=True
            )
            with app.store.connect() as db:
                rows = db.execute("SELECT scope,task_id,snapshot FROM model_tasks").fetchall()
                report["cleanup_jobs"] = [
                    dict(r) for r in db.execute("SELECT task_id,state,error FROM skill_jobs")
                ]
            for row in rows:
                inputs.append(json.loads(row["snapshot"])["workflow"]["input_sha256"])
                if app.skills.store.get(row["scope"], row["task_id"])["job"]:
                    app.skills.store.cancel(row["scope"], row["task_id"])
                    request_stop(app.skills.store, row["task_id"])

        def owned():
            found = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdecimal() or int(entry.name) == os.getpid():
                    continue
                try:
                    if entry.stat().st_uid != os.getuid():
                        continue
                    args = (entry / "cmdline").read_bytes().split(b"\0")
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                if any(
                    str(home).encode() in arg or str(home.relative_to(ROOT)).encode() in arg
                    for arg in args
                ):
                    found.append((int(entry.name), process_start(int(entry.name))))
            return found

        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid, birth in owned():
                if birth and process_start(pid) == birth:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            for _ in range(30):
                if not owned():
                    break
                await asyncio.sleep(0.1)
        assert not owned(), "probe_owned_processes_remaining"
        mongo = AsyncMongoClient(
            os.environ.get("FLIGHT_MONGO_URI")
            or values.get("FLIGHT_MONGO_URI")
            or "mongodb://127.0.0.1:27017"
        )
        dbname = os.environ.get("FLIGHT_MONGO_DB") or values.get("FLIGHT_MONGO_DB") or "flight_data"
        try:
            for name in collections:
                assert name in {"analysis_runs_s5_" + identity, "analysis_artifacts_s5_" + identity}
                await mongo[dbname].drop_collection(name)
        finally:
            await mongo.close()
        for identity in inputs:
            path = ROOT / "var/workflow-inputs" / (identity + ".json")
            assert (
                path.parent.resolve() == (ROOT / "var/workflow-inputs").resolve()
                and not path.is_symlink()
            )
            path.unlink(missing_ok=True)
        assert home.resolve().is_relative_to((ROOT / ".tmp/s5").resolve()) and not home.is_symlink()
        shutil.rmtree(home)
        report.update(
            owned_processes_remaining=0,
            temporary_home_removed=True,
            isolated_collections_removed=True,
        )


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
        path = ROOT / "docs/validation" / ("20261023-S5-native-" + uuid4().hex[:10] + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(path), "status": report["status"]}), flush=True)
