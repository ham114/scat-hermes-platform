"""Real planner -> explicit HTTP approval -> independent Hermes worker -> isolated Mongo result."""

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
            await asyncio.to_thread(child.wait, timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)


async def events(client, path, *, disconnect=False):
    output = []
    async with client.stream("GET", path, timeout=180) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                value = json.loads(line[6:])
                output.append(value)
                if disconnect or value.get("event") in {
                    "run.completed",
                    "run.failed",
                    "run.cancelled",
                    "run.interrupted",
                    "stream.error",
                }:
                    break
    return output


async def probe(report):
    from dotenv import dotenv_values

    from application.context import ExecutionContext
    from application.skill_job_factory import create_skill_tasks
    from scat_model_runtime.step_codec import decode

    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        for port in [18093, 18094, 18095]:
            try:
                await client.get(f"http://127.0.0.1:{port}/health")
            except httpx.TransportError:
                pass
            else:
                raise RuntimeError("s4_port_in_use")

    identity = uuid4().hex[:10]
    home = ROOT / ".tmp/s4" / ("real-" + identity)
    home.mkdir(parents=True)
    collections = ["analysis_runs_s4_" + identity, "analysis_artifacts_s4_" + identity]
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "TMPDIR": str(home),
        "TMP": str(home),
        "TEMP": str(home),
        "HERMES_BASE_URL": "http://127.0.0.1:18094",
        "HERMES_STATE_FILE": str(home / "platform.sqlite"),
        "HERMES_TASK_STATE_FILE": str(home / "jobs.sqlite"),
        "HERMES_RESULT_STATE_FILE": str(home / "results.sqlite"),
        "SCAT_S4_CPU_FIXTURE": "1",
        "HERMES_SKILL_JOBS_ENABLED": "true",
        "ANALYSIS_IDENTITY_MODE": "single_user",
        "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
        "ANALYSIS_RUNS_COLLECTION": collections[0],
        "ANALYSIS_ARTIFACTS_COLLECTION": collections[1],
        "SCAT_GBAR_V23_ENABLED": "false",
        "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
        "SCAT_S4_CPU_DELAY_SECONDS": "15",
        "SCAT_JOB_TEMP_DIR": str(home / "worker-tmp"),
    }
    children = []
    counter = 0
    report["probe_id"] = identity
    report["stage"] = "startup"

    async def launch(command, name, health=None):
        nonlocal counter
        counter += 1
        path = (
            ROOT
            / "var/validation"
            / ("s4-real-" + identity + "-" + str(counter) + "-" + name + ".log")
        )
        with path.open("x", encoding="utf-8") as log:
            path.chmod(0o600)
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
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                for _ in range(120):
                    if child.poll() is not None:
                        raise RuntimeError(name + "_exited")
                    try:
                        r = await client.get(health)
                        if r.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError(name + "_health_timeout")
        return child

    app_command = [APP, "-B", str(ROOT / "scripts/run_platform_api.py"), "--port", "18093"]
    main_command = [
        HERMES,
        "-B",
        str(ROOT / "scripts/run_hermes_api.py"),
        "--home",
        str((home / "main").relative_to(ROOT)),
        "--port",
        "18094",
        "--with-mcp",
        "--synthetic-mcp",
        "--with-model-tasks",
        "--with-skills",
        "--s4-cpu-fixture",
    ]
    values = dotenv_values(ROOT / ".env")
    headers = (
        {"Authorization": "Bearer " + values["AUTH_SECRET"]} if values.get("AUTH_SECRET") else {}
    )
    try:
        # Let the finite acceptance driver stop the probe through its cleanup path.
        task = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        main = await launch(main_command, "main", "http://127.0.0.1:18094/health")
        platform = await launch(app_command, "api", "http://127.0.0.1:18093/health/live")
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18093", headers=headers, trust_env=False, timeout=180
        ) as client:
            thread = await client.post("/api/v1/hermes/threads", json={})
            thread.raise_for_status()
            tid = thread.json()["thread_id"]
            base = f"/api/v1/hermes/threads/{tid}"
            started = time.monotonic()
            response = await client.post(
                base + "/runs",
                json={
                    "request_id": str(uuid4()),
                    "message": "合成 CPU 展示：先列技能并加载 scat-model-analysis，再查询模型目录，准备 synthetic-cpu-cv:1.0.0 的 trajectory_prediction 任务。输入 dataset=scat、batch=SYNTH-CPU、external_id=SYNTH-CPU-001、limit=4，空参数。只实际准备一次任务，给出页面确认提示和任务 ID，勿直接运行模型，勿调用其他 MCP。",
                },
            )
            response.raise_for_status()
            parent_run = response.json()["run_id"]
            stream = base + "/runs/" + parent_run + "/events"
            first = await events(client, stream, disconnect=True)
            assert first
            # Reconnecting SSE is independent of the persisted proposal/job lifetime.
            tail = await events(client, stream)
            assert tail[-1]["event"] == "run.completed"
            tasks = await client.get(base + "/model-tasks")
            tasks.raise_for_status()
            items = tasks.json()["items"]
            assert len(items) == 1 and items[0]["confirmation_required"]
            view = items[0]
            task_id = view["task_id"]
            report.update(stage="approved_worker", task_id=task_id, parent_run_id=parent_run)
            with sqlite3.connect(home / "jobs.sqlite") as db:
                assert (
                    db.execute("SELECT count(*) FROM skill_backend_executions").fetchone()[0] == 0
                )
            endpoint = base + "/model-tasks/" + task_id
            approval = {"snapshot_sha256": view["snapshot_sha256"], "confirmed": True}
            confirmed = await client.post(endpoint + "/confirm", json=approval)
            confirmed.raise_for_status()
            replay = await client.post(endpoint + "/confirm", json=approval)
            replay.raise_for_status()
            assert confirmed.json()["task_id"] == replay.json()["task_id"]
            worker = await launch(
                [
                    APP,
                    "-B",
                    str(ROOT / "scripts/run_model_worker.py"),
                    "--job-home",
                    str((home / "background").relative_to(ROOT)),
                    "--background-port",
                    "18095",
                    "--lease-seconds",
                    "6",
                ],
                "worker",
            )
            # The parent service answers a separate conversation while the job worker is alive.
            reply = await client.post(
                base + "/runs",
                json={
                    "request_id": str(uuid4()),
                    "message": "不调用工具，用一句中文解释 RMSE 的含义。不要准备新任务或运行模型。",
                },
            )
            reply.raise_for_status()
            parallel = await events(client, base + "/runs/" + reply.json()["run_id"] + "/events")
            assert parallel[-1]["event"] == "run.completed"
            report["parent_dialogue_available_during_job"] = worker.poll() is None
            await stop(platform)
            await stop(main)
            report["chat_services_stopped_before_job_completion"] = worker.poll() is None
            # Crash the worker and its private native/tool clients after the independent
            # backend actually starts. The CPU backend has its own process group.
            crash_deadline = time.monotonic() + 180
            while time.monotonic() < crash_deadline:
                with sqlite3.connect(home / "jobs.sqlite") as db:
                    running = db.execute(
                        "SELECT state FROM skill_backend_executions WHERE task_id=?", (task_id,)
                    ).fetchone()
                if running and running[0] == "running":
                    break
                if worker.poll() is not None:
                    raise RuntimeError("worker_exited_before_cpu_start")
                await asyncio.sleep(0.05)
            assert running and running[0] == "running"
            os.killpg(worker.pid, signal.SIGKILL)
            await asyncio.to_thread(worker.wait, timeout=10)
            await asyncio.sleep(7)  # actual lease expiry, not a database edit
            worker = await launch(
                [
                    APP,
                    "-B",
                    str(ROOT / "scripts/run_model_worker.py"),
                    "--job-home",
                    str((home / "background").relative_to(ROOT)),
                    "--background-port",
                    "18095",
                    "--lease-seconds",
                    "6",
                ],
                "worker-restarted",
            )
            report["worker_process_group_crashed_and_restarted"] = True
            report["stage"] = "recovery"
            # No UI/API process now exists. Observe the durable outbox, not an SSE response.
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                with sqlite3.connect(home / "jobs.sqlite") as db:
                    job = db.execute(
                        "SELECT state,error FROM skill_jobs WHERE task_id=?", (task_id,)
                    ).fetchone()
                if job and job[0] in {"succeeded", "failed", "paused", "cancelled"}:
                    break
                if worker.poll() is not None and (not job or job[0] != "succeeded"):
                    raise RuntimeError("worker_exited_without_verified_job")
                await asyncio.sleep(0.2)
            assert job and job[0] == "succeeded", job
            await stop(worker)
            # Recreate API; task and final artifacts remain readable even while main Hermes is down.
            platform = await launch(
                app_command, "api-restarted", "http://127.0.0.1:18093/health/live"
            )
            result = await client.get(endpoint)
            result.raise_for_status()
            final = result.json()
            assert (
                final["skill_job"]["state"] == "succeeded" and final["run"]["status"] == "succeeded"
            )
            artifact_id = final["run"]["result_artifact_ids"][0]
            artifact = await client.get(endpoint + "/artifacts/" + artifact_id)
            artifact.raise_for_status()
            assert artifact.json()["analysis_result"]["schema_version"] == "1.4"
            other = await client.post("/api/v1/hermes/threads", json={})
            other.raise_for_status()
            denied = await client.get(
                f"/api/v1/hermes/threads/{other.json()['thread_id']}/model-tasks/{task_id}"
            )
            assert denied.status_code == 404
            controller = create_skill_tasks(ROOT, home / "jobs.sqlite", cpu_fixture=True)
            context = ExecutionContext("scat-researcher", tid)
            row = controller.skills.store.get(context.scope_key, task_id)
            body = controller.skills.read_result(context, task_id)
            state = decode(body["state"])
            assert state["regression_output"].series[0].values == ((40, 80, 1020), (50, 100, 1025))
            assert [e.mae for e in state["regression_evaluation"].target_errors] == [1, 0.5, 0]
            with controller.store.connect() as db:
                backends = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,state,calls,output_sha FROM skill_backend_executions WHERE task_id=?",
                        (task_id,),
                    )
                ]
                sessions = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,scope,native_run,revision,generation FROM skill_job_sessions WHERE task_id=?",
                        (task_id,),
                    )
                ]
                budgets = [dict(r) for r in db.execute("SELECT * FROM skill_run_budgets")]
            assert (
                len(backends) == 1
                and backends[0]["calls"] == 1
                and backends[0]["state"] == "succeeded"
            )
            assert sessions and len(budgets) == 1 and budgets[0]["finished_at"] is not None
            with sqlite3.connect(home / "main/skill-loads.sqlite") as db:
                db.row_factory = sqlite3.Row
                parent_budget = dict(
                    db.execute(
                        "SELECT * FROM skill_run_budgets WHERE run_id=?", (parent_run,)
                    ).fetchone()
                )
            assert (
                budgets[0]["llm_calls"] > parent_budget["llm_calls"]
                and budgets[0]["tool_calls"] >= parent_budget["tool_calls"] + 7
            )
            # Terminalization fences the generation; locate the actual session home by existing trace.
            traces = list((home / "background" / task_id).glob("*/skill-loads.sqlite"))
            assert len(traces) >= 2
            operations = []
            matches = 0
            for trace in traces:
                with sqlite3.connect(trace) as db:
                    operations.extend(
                        db.execute(
                            "SELECT operation,skill_id,load_id,outcome FROM skill_operations"
                        ).fetchall()
                    )
                    matches += db.execute(
                        "SELECT count(*) FROM skill_operations o JOIN skill_loads l ON o.load_id=l.id AND o.scope=l.scope AND o.run_id=l.run_id AND o.session_id=l.session_id AND o.file_sha256=l.file_sha256 WHERE l.file_path='SKILL.md'"
                    ).fetchone()[0]
            assert {r[0] for r in operations} == {
                "scat_model_prepare_input",
                "scat_model_run",
                "scat_model_evaluate",
                "scat_model_save_result",
                "scat_model_read_result",
            }
            # SIGKILL deliberately leaves an admitted operation without a reply.
            # It still requires a valid current-body load; recovered returned steps
            # must independently cover all five operations.
            assert matches == len(operations)
            assert {r[0] for r in operations if r[3] == "returned"} == {
                "scat_model_prepare_input",
                "scat_model_run",
                "scat_model_evaluate",
                "scat_model_save_result",
                "scat_model_read_result",
            }
            assert len({s["generation"] for s in sessions}) >= 2
            assert len(traces) == len({s["generation"] for s in sessions})
            assert (
                report["parent_dialogue_available_during_job"]
                and report["chat_services_stopped_before_job_completion"]
            )
            report.update(
                status="passed",
                elapsed_seconds=round(time.monotonic() - started, 2),
                task_id=task_id,
                parent_run_id=parent_run,
                exact_background_tools=7,
                job=final["skill_job"],
                parent_budget=parent_budget,
                shared_job_budget=budgets[0],
                delegated_sessions=sessions,
                backend_executions=backends,
                skill_operations=operations,
                matching_skill_operations=matches,
                interrupted_operations=sum(r[3] == "admitted" for r in operations),
                artifact=artifact.json(),
                input_sha256=row["snapshot"]["skills"]["input_sha256"],
                snapshot_sha256=row["sha256"],
                synthetic=True,
                business_dataset_queries=0,
                isolated_mongo_roundtrip=True,
                production_gpu=False,
                cross_thread_denied=True,
            )
    finally:
        for child in reversed(children):
            await stop(child)
        # Detached CPU/native descendants may outlive a killed worker. Match this
        # probe's private absolute home, never a shared port or generic process name.
        from application.skill_cpu_backend import process_start, request_stop

        if (home / "jobs.sqlite").exists():
            cleanup = create_skill_tasks(ROOT, home / "jobs.sqlite", cpu_fixture=True)
            with cleanup.store.connect() as db:
                rows = db.execute(
                    "SELECT t.scope,t.task_id FROM model_tasks t JOIN skill_jobs j ON j.task_id=t.task_id"
                ).fetchall()
                report["cleanup_jobs"] = [
                    dict(r) for r in db.execute("SELECT task_id,state,error FROM skill_jobs")
                ]
                report["cleanup_backends"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,state,calls,pid,process_start FROM skill_backend_executions"
                    )
                ]
            for row in rows:
                cleanup.skills.store.cancel(row["scope"], row["task_id"])
                request_stop(cleanup.skills.store, row["task_id"])

        def owned_processes():
            found = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdecimal():
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
            for pid, birth in owned_processes():
                if birth and process_start(pid) == birth:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            for _ in range(40):
                if not owned_processes():
                    break
                await asyncio.sleep(0.1)
        assert not owned_processes(), "owned_process_cleanup_incomplete"
        report["owned_processes_remaining"] = 0
        # Drop only the two uniquely named synthetic validation collections.
        from dotenv import dotenv_values
        from pymongo import AsyncMongoClient

        config = dotenv_values(ROOT / ".env")
        uri = (
            os.environ.get("FLIGHT_MONGO_URI")
            or config.get("FLIGHT_MONGO_URI")
            or "mongodb://127.0.0.1:27017"
        )
        dbname = os.environ.get("FLIGHT_MONGO_DB") or config.get("FLIGHT_MONGO_DB") or "flight_data"
        mongo = AsyncMongoClient(uri)
        try:
            for name in collections:
                assert name in {"analysis_runs_s4_" + identity, "analysis_artifacts_s4_" + identity}
                await mongo[dbname].drop_collection(name)
        finally:
            await mongo.close()
        assert home.resolve().is_relative_to((ROOT / ".tmp/s4").resolve()) and not home.is_symlink()
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()
        report["isolated_collections_removed"] = True


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda")
    report = {"status": "running"}
    try:
        asyncio.run(probe(report))
    except asyncio.CancelledError:
        report.update(status="cancelled", error_type="CancelledError")
        raise
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        path = ROOT / "docs/validation" / ("20261023-S4-hermes-" + uuid4().hex[:10] + ".json")
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"report": str(path), "status": report["status"]}))
