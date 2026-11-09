"""Authenticated MCP and finite action RPC; no commands, environment or paths accepted."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "tasks": "run_model_task_tool.py",
    "steps": "run_model_step_tool.py",
    "workflows": "run_workflow_tool.py",
}


def role_for(headers, keys):
    value = headers.get("authorization", "")
    return next(
        (role for role, token in keys.items() if secrets.compare_digest(value, "Bearer " + token)),
        None,
    )


def allowed(role, namespace, payload):
    from hermes_runtime.model_step_bridge import MODEL_STEP_TOOLS
    from hermes_runtime.task_bridge import TASK_TOOLS
    from workflow_runtime.catalog import CONTROLS, OPERATIONS

    if not isinstance(payload, dict) or set(payload) - {
        "operation",
        "arguments",
        "scope",
        "source_run_id",
        "proposal_key",
        "job_session",
    }:
        return False
    if not isinstance(payload.get("arguments"), dict) or not all(
        isinstance(payload.get(k), str) and 1 <= len(payload[k]) <= 200
        for k in ["scope", "source_run_id"]
    ):
        return False
    operation = payload.get("operation")
    if role == "main":
        return "job_session" not in payload and (
            (namespace == "tasks" and operation in TASK_TOOLS)
            or (namespace == "workflows" and operation in CONTROLS)
        )
    return (
        role == "worker"
        and isinstance(payload.get("job_session"), str)
        and bool(payload["job_session"])
        and (
            (namespace == "steps" and operation in MODEL_STEP_TOOLS)
            or (namespace == "workflows" and operation in OPERATIONS)
        )
    )


def create_app(keys, application):
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from application.context import ExecutionContext
    from scat_mcp.server import create_server

    manager = StreamableHTTPSessionManager(
        create_server(
            application,
            lambda: ExecutionContext(
                os.environ.get("ANALYSIS_SINGLE_USER_ID", "scat-researcher"), "mcp-readonly"
            ),
        ),
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*"],
            allowed_origins=["http://127.0.0.1:*"],
        ),
    )
    semaphore = asyncio.Semaphore(4)

    async def health(request):
        return JSONResponse({"status": "ready", "service": "scat-actions-mcp", "synthetic": False})

    async def schemas(request):
        role = role_for(request.headers, keys)
        if role is None:
            return JSONResponse({"status": "forbidden"}, status_code=401)
        namespace = request.path_params["namespace"]
        if namespace not in SCRIPTS:
            return JSONResponse({"status": "not_found"}, status_code=404)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(ROOT / "scripts" / SCRIPTS[namespace]),
            "--schemas",
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
        if process.returncode:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        rows = json.loads(stdout)
        if namespace == "workflows":
            catalog = json.loads((ROOT / "services/hermes/workflow-operations.v1.json").read_text())
            rows = [
                {k: item[k] for k in ("name", "description", "parameters")}
                for item in catalog["controls" if role == "main" else "operations"]
            ]
        return JSONResponse(rows)

    async def invoke(request):
        role = role_for(request.headers, keys)
        if role is None:
            return JSONResponse({"status": "forbidden"}, status_code=401)
        namespace = request.path_params["namespace"]
        body = await request.body()
        if len(body) > 1048576:
            return JSONResponse({"status": "invalid_request"}, status_code=413)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"status": "invalid_request"}, status_code=400)
        if namespace == "processes":
            if role not in {"worker", "platform"} or not isinstance(payload, dict):
                return JSONResponse({"status": "forbidden"}, status_code=403)
            state = (ROOT / os.environ["HERMES_TASK_STATE_FILE"]).resolve()
            if not state.is_relative_to(ROOT / "var"):
                raise ValueError("state_outside_project")

            def connect():
                db = sqlite3.connect("file:" + str(state) + "?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                return db

            from application.skill_cpu_backend import process_start, request_stop

            with connect() as db:
                if (
                    payload.get("action") == "birth"
                    and set(payload) == {"action", "pid"}
                    and type(payload["pid"]) is int
                ):
                    row = db.execute(
                        "SELECT pid FROM skill_backend_executions WHERE pid=?", (payload["pid"],)
                    ).fetchone()
                    return JSONResponse(
                        {"status": "ok", "birth": process_start(payload["pid"]) if row else None}
                    )
                if payload.get("action") == "stop" and set(payload) == {"action", "task_id"}:
                    row = db.execute(
                        "SELECT state FROM skill_jobs WHERE task_id=?", (payload["task_id"],)
                    ).fetchone()
                    if row and row["state"] == "cancel_requested":
                        from types import SimpleNamespace

                        request_stop(
                            SimpleNamespace(tasks=SimpleNamespace(connect=connect)),
                            payload["task_id"],
                        )
                        return JSONResponse({"status": "ok"})
            return JSONResponse({"status": "forbidden"}, status_code=403)
        if namespace not in SCRIPTS or not allowed(role, namespace, payload):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        try:
            await asyncio.wait_for(semaphore.acquire(), 0.05)
        except TimeoutError:
            return JSONResponse({"status": "busy"}, status_code=429, headers={"Retry-After": "1"})
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                str(ROOT / "scripts" / SCRIPTS[namespace]),
                cwd=ROOT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(json.dumps(payload, ensure_ascii=False).encode()), 125
            )
            if process.returncode or len(stdout) > 524288:
                return JSONResponse(
                    {"status": "unknown", "automatic_replay": False}, status_code=503
                )
            return JSONResponse(json.loads(stdout))
        except (TimeoutError, ValueError):
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            return JSONResponse({"status": "unknown", "automatic_replay": False}, status_code=503)
        finally:
            semaphore.release()

    class MCP:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            if role_for(request.headers, keys) != "main":
                await JSONResponse({"status": "forbidden"}, status_code=401)(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield
        await application.aclose()

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/v1/schemas/{namespace}", schemas),
            Route("/v1/actions/{namespace}", invoke, methods=["POST"]),
            Mount("/mcp", MCP()),
        ],
        lifespan=lifespan,
    )
