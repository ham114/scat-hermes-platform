"""Read-only diagnostic routes behind the existing portal identity boundary."""

import base64
import json
import math
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core import settings
from hermes_runtime.diagnostics import LangfuseReader, configuration, journal, summary
from service.auth import verify_bearer
from service.hermes_api import Owner

router = APIRouter(
    prefix="/api/v1/hermes/diagnostics", tags=["Diagnostics"], dependencies=[Depends(verify_bearer)]
)


def access(request, owner):
    admin = False
    if settings.ANALYSIS_IDENTITY_MODE == "trusted_proxy" and settings.PORTAL_SIGNING_KEY_FILE:
        from hermes_runtime.portal_identity import request_principal

        principal = request_principal(
            request, settings.PORTAL_IDENTITIES_FILE, settings.PORTAL_SIGNING_KEY_FILE
        )
        admin = principal["role"] == "admin"
    return owner, admin


def store():
    try:
        value = journal()
    except (ValueError, OSError):
        raise HTTPException(503, "排查记录暂时不可用") from None
    if value is None:
        raise HTTPException(503, "调用链采集尚未启用")
    return value


def public(row):
    return {key: value for key, value in row.items() if key not in {"scope", "session_id"}}


class RenderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(min_length=1, max_length=128)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    state: Literal["rendered", "failed"]


@router.post("/{trace_id}/render", status_code=202)
async def render_event(trace_id: str, body: RenderEvent, request: Request, owner: Owner):
    import time

    from hermes_runtime.results import ResultNotFoundError
    from service.hermes_api import get_runtime

    try:
        row = store().get(trace_id, owner)
        runtime = get_runtime(request)
        receipt = runtime.results.get(row["scope"], row["run_id"], body.result_id)
        if receipt["payload_sha256"] != body.payload_sha256:
            raise LookupError()
    except (LookupError, ResultNotFoundError, ValueError, AttributeError):
        raise HTTPException(404, "结果不存在") from None
    store().node(
        row["scope"],
        row["run_id"],
        "render:" + body.event_id,
        "页面结果展示",
        state="completed" if body.state == "rendered" else "failed",
        started=time.time(),
        output={"result_id": body.result_id, "payload_sha256": body.payload_sha256},
    )
    return {"status": "accepted"}


@router.get("")
async def traces(
    request: Request,
    owner: Owner,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    state: Annotated[
        str | None,
        Query(pattern=r"^(running|completed|failed|cancelled|interrupted|succeeded|partial)$"),
    ] = None,
    thread_id: Annotated[str | None, Query(max_length=128)] = None,
    run_id: Annotated[str | None, Query(max_length=128)] = None,
):
    import asyncio

    before = None
    if cursor:
        try:
            before = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (
                type(before) is not list
                or len(before) != 2
                or type(before[0]) not in (int, float)
                or not math.isfinite(before[0])
                or not re.fullmatch(r"[a-f0-9]{32}", before[1])
            ):
                raise ValueError()
        except (ValueError, TypeError, IndexError):
            raise HTTPException(422, "分页位置无效") from None
    user, admin = access(request, owner)
    rows, more = await asyncio.to_thread(
        store().list,
        user,
        admin=admin,
        limit=limit,
        before=before,
        state=state,
        thread_id=thread_id,
        run_id=run_id,
    )
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps([rows[-1]["created"], rows[-1]["id"]]).encode())
        .decode()
        .rstrip("=")
        if more
        else None
    )
    return {"items": [public(row) for row in rows], "next_cursor": next_cursor}


@router.get("/{trace_id}")
async def trace(trace_id: str, request: Request, owner: Owner):
    import asyncio

    if not re.fullmatch(r"[a-f0-9]{32}", trace_id):
        raise HTTPException(404, "记录不存在")
    db = store()
    user, admin = access(request, owner)
    try:
        row = await asyncio.to_thread(db.get, trace_id, user, admin=admin)
    except LookupError:
        raise HTTPException(404, "记录不存在") from None
    runtime = getattr(request.app.state, "hermes_runtime", None)
    if row["state"] == "running" and row["owner"] and row["thread_id"] and runtime is not None:
        from application.context import ExecutionContext
        from hermes_runtime.client import HermesRuntimeError

        try:
            async with asyncio.timeout(3):
                await runtime.get_run(
                    ExecutionContext(row["owner"], row["thread_id"]), row["run_id"]
                )
            row = db.get(trace_id, user, admin=admin)
        except (HermesRuntimeError, LookupError, TimeoutError):
            pass
    local = await asyncio.to_thread(db.nodes, trace_id)
    remote, status = await LangfuseReader(configuration()).observations(row["session_id"])
    for node in local:
        if node["id"].startswith("result:"):
            call = node["id"].removeprefix("result:")
            parent = next(
                (item for item in remote if item.get("details", {}).get("tool_call_id") == call),
                None,
            )
            if parent:
                node["parent_id"] = parent["id"]
    return {
        "trace": public(row),
        "nodes": [*local, *remote],
        "related_tasks": db.related_tasks(row),
        "langfuse_status": status,
        "complete": status == "available"
        and row["state"]
        in {"completed", "failed", "cancelled", "interrupted", "succeeded", "partial"},
    }


@router.get("/tasks/{task_id}")
async def task_trace(task_id: str, request: Request, owner: Owner):
    import asyncio
    import sqlite3
    from contextlib import closing

    from application.context import ExecutionContext
    from application.task_store import TaskNotFound
    from service.model_task_api import get_tasks

    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise HTTPException(404, "记录不存在")
    user, admin = access(request, owner)
    tasks = get_tasks(request)
    try:
        try:
            link = await asyncio.to_thread(store().task, task_id, user, admin=admin)
        except LookupError:
            # Historical tasks retain their verified snapshot ownership even without telemetry.
            with closing(tasks.store.connect()) as db:
                historical = db.execute(
                    "SELECT scope FROM model_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            if historical is None:
                raise TaskNotFound()
            old = tasks.store.get(historical[0], task_id)
            snapshot = old["snapshot"]
            requested_by = snapshot.get("request", {}).get("requested_by") or snapshot["run"].get(
                "owner_user_id"
            )
            if not requested_by:
                raise TaskNotFound()
            if requested_by != user and not admin:
                raise TaskNotFound()
            link = {
                "scope": historical[0],
                "owner": requested_by,
                "thread_id": old["snapshot"]["source"]["thread_id"],
                "source_run": old["snapshot"]["source"].get("run_id"),
            }
        row = await asyncio.to_thread(tasks.store.get, link["scope"], task_id)
    except (LookupError, TaskNotFound):
        raise HTTPException(404, "记录不存在") from None
    context = ExecutionContext(link["owner"], link["thread_id"])
    view = await tasks.get(context, task_id)
    nodes, sessions = [], []
    if tasks.skills is not None and "skills" in row["snapshot"]:
        with closing(tasks.store.connect()) as db:
            db.row_factory = sqlite3.Row
            for item in db.execute(
                "SELECT * FROM skill_job_events WHERE task_id=? ORDER BY id LIMIT 100", (task_id,)
            ):
                nodes.append(
                    {
                        "id": "event:" + str(item["id"]),
                        "parent_id": None,
                        "name": item["kind"],
                        "kind": "event",
                        "state": "recorded",
                        "started": item["at"],
                        "ended": item["at"],
                        "input": {},
                        "output": summary(json.loads(item["data"])),
                        "source": "platform",
                    }
                )
            for item in db.execute(
                "SELECT * FROM skill_backend_executions WHERE task_id=? ORDER BY started LIMIT 100",
                (task_id,),
            ):
                nodes.append(
                    {
                        "id": item["id"],
                        "parent_id": None,
                        "name": item["step_key"],
                        "kind": "model-step",
                        "state": item["state"],
                        "started": item["started"],
                        "ended": item["finished"],
                        "input": summary(json.loads(item["request"])),
                        "output": {
                            "execution_count": item["calls"],
                            "failed": item["error"] is not None,
                        },
                        "source": "platform",
                    }
                )
            sessions = db.execute(
                "SELECT DISTINCT native_session FROM skill_job_sessions WHERE task_id=? ORDER BY native_session LIMIT 10",
                (task_id,),
            ).fetchall()
    remote_status = "pending"
    reader = LangfuseReader(configuration())
    for session in sessions:
        remote, status = await reader.observations(session[0])
        nodes.extend(remote)
        if status == "unavailable" or remote_status == "pending":
            remote_status = status
    state = (
        (view.get("skill_job") or {}).get("state")
        or (view.get("run") or {}).get("status")
        or row["state"]
    )
    return {
        "trace": {
            "id": task_id,
            "run_id": row["run_id"],
            "owner": link["owner"],
            "thread_id": link["thread_id"],
            "request_id": None,
            "profile": "scat-worker",
            "state": state,
            "created": row["created"],
            "updated": row["created"],
        },
        "nodes": nodes,
        "related_tasks": [],
        "langfuse_status": remote_status,
        "complete": False,
        "task_id": task_id,
        "source_run": link["source_run"],
    }
