"""Native job control in its own Conda/container; leases never come from model tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def create_app(key):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from application.task_store import TaskConflict
    from deployment_runtime.actions import role_for
    from hermes_runtime.job_context import load_approved_job

    children = {}
    state = (ROOT / os.environ["HERMES_TASK_STATE_FILE"]).resolve()
    if not state.is_relative_to(ROOT / "var"):
        raise ValueError("job_state_outside_project")
    directory = ROOT / "var/deploy/h7b/shared/job-control"
    directory.mkdir(parents=True, exist_ok=True)
    port = 18128

    async def health(request):
        return JSONResponse(
            {
                "status": "ready",
                "service": "hermes-job-control",
                "active": sum(c.poll() is None for c in children.values()),
            }
        )

    async def command(request):
        if role_for(request.headers, {"worker": key}) != "worker":
            return JSONResponse({"status": "forbidden"}, status_code=401)
        raw = await request.body()
        if len(raw) > 4096:
            return JSONResponse({"status": "invalid_request"}, status_code=413)
        try:
            value = json.loads(raw)
        except ValueError:
            return JSONResponse({"status": "invalid_request"}, status_code=400)
        if (
            not isinstance(value, dict)
            or set(value) != {"task_id", "worker", "generation", "delegation"}
            or not all(isinstance(value[k], str) for k in ["task_id", "worker", "delegation"])
            or type(value["generation"]) is not int
        ):
            return JSONResponse({"status": "invalid_request"}, status_code=400)
        if (
            not re.fullmatch("[0-9a-f]{32}", value["task_id"])
            or not 1 <= value["generation"] <= 1000000
        ):
            return JSONResponse({"status": "invalid_request"}, status_code=400)
        identity = value["task_id"] + "-" + str(value["generation"])
        marker = directory / (identity + ".json")
        child = children.get(identity)
        if request.path_params["action"] == "stop":
            if (
                not marker.exists()
                or json.loads(marker.read_text()).get("worker") != value["worker"]
            ):
                return JSONResponse({"status": "forbidden"}, status_code=403)
            if child and child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait)
            return JSONResponse({"status": "stopped"})
        try:
            jobs, _row = load_approved_job(
                ROOT, state, value["task_id"], value["worker"], value["generation"]
            )
            with jobs.tasks.connect() as db:
                delegation = db.execute(
                    "SELECT * FROM skill_job_delegations WHERE id=? AND task_id=? AND revoked=0 AND expires>?",
                    (value["delegation"], value["task_id"], jobs.clock()),
                ).fetchone()
            if delegation is None:
                raise ValueError("delegation_missing")
        except (ValueError, KeyError, TaskConflict):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        if request.path_params["action"] != "start":
            return JSONResponse({"status": "not_found"}, status_code=404)
        if marker.exists():
            return JSONResponse(
                {
                    "status": "started" if child and child.poll() is None else "unknown",
                    "api_port": port,
                    "automatic_replay": False,
                }
            )
        if any(c.poll() is None for c in children.values()):
            return JSONResponse({"status": "busy"}, status_code=409)
        home = (
            ROOT / "var/deploy/h7b/shared/homes/jobs" / value["task_id"] / str(value["generation"])
        )
        home.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "task_id": value["task_id"],
                    "worker": value["worker"],
                    "generation": value["generation"],
                    "state": "starting",
                }
            )
        )
        args = [
            sys.executable,
            "-B",
            str(ROOT / "scripts/run_hermes_api.py"),
            "--profile",
            "scat-worker",
            "--home",
            str(home.relative_to(ROOT)),
            "--port",
            str(port),
            "--with-skills",
            "--background-job",
            value["task_id"],
            "--job-state",
            str(state),
            "--job-worker",
            value["worker"],
            "--job-generation",
            str(value["generation"]),
            "--job-delegation",
            value["delegation"],
        ]
        with (directory / (identity + ".log")).open("a") as log:
            child = await asyncio.to_thread(
                subprocess.Popen,
                args,
                cwd=ROOT,
                env=os.environ | {"SCAT_ACTION_KEY_FILE": "var/deploy/h7b/secrets/rpc-worker.key"},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        children[identity] = child

        async def supervise():
            while child.poll() is None:
                await asyncio.sleep(2)
                try:
                    jobs.require_lease(value["task_id"], value["worker"], value["generation"])
                except TaskConflict:
                    child.terminate()
                    break

        task = asyncio.create_task(supervise())
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return JSONResponse({"status": "started", "api_port": port})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            for child in children.values():
                try:
                    await asyncio.to_thread(child.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait)

    return Starlette(
        routes=[Route("/health", health), Route("/v1/jobs/{action}", command, methods=["POST"])],
        lifespan=lifespan,
    )
