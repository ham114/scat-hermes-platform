"""Owner-scoped Hermes runs; upstream credentials and native sessions stay private."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from application.context import ExecutionContext
from core import settings
from core.settings import AnalysisIdentityMode
from hermes_runtime.client import HermesHTTPRuntime, HermesRuntimeError
from hermes_runtime.registry import (
    RequestConflictError,
    RunNotFoundError,
    ThreadBusyError,
    ThreadNotFoundError,
)
from hermes_runtime.results import ResultNotFoundError
from service.auth import verify_bearer
from service.identity import get_analysis_owner_id
from service.request_context import get_request_id

router = APIRouter(prefix="/api/v1/hermes", tags=["Hermes"], dependencies=[Depends(verify_bearer)])
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
RUN_FIELDS = {"run_id", "status", "output", "completed", "partial", "interrupted", "replayed"}
EVENT_FIELDS = {
    "business.result": {
        "result_id",
        "operation",
        "payload_sha256",
        "created_at",
        "expires_at",
        "kind",
    },
    "message.delta": {"delta"},
    "message.interim": {"text", "already_streamed"},
    "tool.started": {"tool", "tool_call_id"},
    "tool.completed": {"tool", "tool_call_id", "duration", "error"},
    "run.completed": {"output", "completed", "partial", "interrupted"},
    "run.failed": {"completed", "partial", "interrupted"},
    "run.cancelled": {"completed", "partial", "interrupted"},
    "run.interrupted": {"completed", "partial", "interrupted"},
}


def get_runtime(request: Request) -> HermesHTTPRuntime:
    runtime = getattr(request.app.state, "hermes_runtime", None)
    if runtime is None:
        raise HTTPException(503, "Hermes runtime is unavailable")
    return runtime


def get_hermes_owner(
    value: Annotated[str | None, Header(alias="X-Authenticated-User")] = None,
    request: Request = None,
) -> str:
    if (
        settings.ANALYSIS_IDENTITY_MODE == AnalysisIdentityMode.TRUSTED_PROXY
        and not settings.PORTAL_SIGNING_KEY_FILE
    ):
        raise HTTPException(503, "Hermes requires a verified portal identity configuration")
    return get_analysis_owner_id(value, request)


Runtime = Annotated[HermesHTTPRuntime, Depends(get_runtime)]
Owner = Annotated[str, Depends(get_hermes_owner)]
PageLimit = Annotated[int, Query(ge=1, le=100)]


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    message: str = Field(min_length=1, max_length=32000)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadTitleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


def _error(exc: HermesRuntimeError) -> JSONResponse:
    code = {"upstream_conflict": 409, "upstream_rate_limited": 429, "upstream_not_found": 404}.get(
        exc.code, 503
    )
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": exc.code,
                "message": "Hermes request could not be completed",
                "request_id": get_request_id(),
                "outcome_unknown": exc.outcome_unknown,
            }
        },
    )


async def _context(runtime, owner, thread_id) -> ExecutionContext:
    try:
        return await asyncio.to_thread(runtime.registry.require_thread, owner, str(thread_id))
    except ThreadNotFoundError:
        raise HTTPException(404, "Thread was not found") from None


def _view(data):
    return {key: value for key, value in data.items() if key in RUN_FIELDS}


def _result_store(runtime):
    if runtime.results is None:
        raise HTTPException(503, "Read-only result store is unavailable")
    return runtime.results


async def _references(runtime, context, run_id):
    if runtime.results is None:
        return []
    return await asyncio.to_thread(runtime.results.list, context.scope_key, run_id)


async def _poll(runtime, context, run_id):
    try:
        data = await runtime.get_run(context, run_id)
    except RunNotFoundError:
        raise HTTPException(404, "Run was not found") from None
    if data.get("status") == "completed" and runtime.results is not None:
        from hermes_runtime.trajectory_reply import coverage, normalize_reply

        facts = await asyncio.to_thread(coverage, runtime.results, context.scope_key, run_id)
        data = {**data, "output": normalize_reply(data.get("output", ""), facts)}
    if data.get("status") in TERMINAL:
        await asyncio.to_thread(runtime.registry.finish_run, context, run_id)
    await asyncio.to_thread(runtime.registry.save_turn, context, _view(data))
    return data


@router.post("/threads", status_code=201)
async def create_thread(body: EmptyInput, runtime: Runtime, owner: Owner):
    return await asyncio.to_thread(runtime.registry.create_thread, owner)


@router.get("/threads")
async def list_threads(
    runtime: Runtime,
    owner: Owner,
    limit: PageLimit = 20,
    cursor: str | None = Query(default=None, max_length=512),
):
    try:
        return await asyncio.to_thread(runtime.registry.history_threads, owner, limit, cursor)
    except ValueError:
        raise HTTPException(422, "Invalid history cursor") from None


@router.patch("/threads/{thread_id}")
async def rename_thread(thread_id: UUID, body: ThreadTitleInput, runtime: Runtime, owner: Owner):
    await _context(runtime, owner, thread_id)
    try:
        return await asyncio.to_thread(
            runtime.registry.rename_thread,
            owner,
            str(thread_id),
            body.title,
            body.expected_revision,
        )
    except ValueError as exc:
        raise HTTPException(409 if str(exc) == "thread_title_conflict" else 422, str(exc)) from None


@router.get("/threads/{thread_id}/messages")
async def messages(
    thread_id: UUID,
    runtime: Runtime,
    owner: Owner,
    limit: PageLimit = 20,
    cursor: str | None = Query(default=None, max_length=512),
):
    context = await _context(runtime, owner, thread_id)
    try:
        page = await asyncio.to_thread(runtime.registry.history_turns, context, limit, cursor)
    except ValueError:
        raise HTTPException(422, "Invalid history cursor") from None
    # Legacy answers can be recovered by read-only upstream polling, never by POST.
    semaphore = asyncio.Semaphore(4)

    async def refresh(item):
        if item["status"] not in TERMINAL and item["run_id"]:
            try:
                async with semaphore:
                    await _poll(runtime, context, item["run_id"])
            except (HermesRuntimeError, HTTPException):
                pass

    await asyncio.gather(*(refresh(item) for item in page["items"]))
    page = await asyncio.to_thread(runtime.registry.history_turns, context, limit, cursor)
    if runtime.results is not None:
        from hermes_runtime.trajectory_reply import coverage, normalize_reply

        for item in page["items"]:
            if item["status"] == "completed" and item.get("run_id") and item.get("output"):
                facts = await asyncio.to_thread(
                    coverage, runtime.results, context.scope_key, item["run_id"]
                )
                item["output"] = normalize_reply(item["output"], facts)
    return page


@router.get("/threads/{thread_id}/runs")
async def list_runs(thread_id: UUID, runtime: Runtime, owner: Owner, limit: PageLimit = 20):
    context = await _context(runtime, owner, thread_id)
    return {"items": await asyncio.to_thread(runtime.registry.list_runs, context, limit)}


@router.post("/threads/{thread_id}/runs", status_code=202)
async def create_run(thread_id: UUID, body: RunInput, runtime: Runtime, owner: Owner):
    context = await _context(runtime, owner, thread_id)
    # Reconcile a prior run that finished while no browser was watching.
    active = await asyncio.to_thread(runtime.registry.active_run, context)
    if active:
        try:
            await _poll(runtime, context, active)
        except HermesRuntimeError as exc:
            return _error(exc)
    digest = hashlib.sha256(body.message.encode()).hexdigest()
    try:
        await asyncio.to_thread(
            runtime.registry.reserve_request, context, str(body.request_id), digest, body.message
        )
    except (ThreadBusyError, RequestConflictError):
        raise HTTPException(409, "Thread is busy or request payload changed") from None
    try:
        data = await runtime.create_run(context, body.message, request_id=body.request_id)
    except HermesRuntimeError as exc:
        if exc.code in {
            "upstream_bad_request",
            "upstream_invalid_request",
            "upstream_auth_failed",
            "upstream_forbidden",
            "upstream_not_found",
            "upstream_conflict",
            "upstream_rate_limited",
        }:
            await asyncio.to_thread(
                runtime.registry.release_rejected_request, context, str(body.request_id)
            )
        else:
            # Admission may already have happened even if no valid receipt arrived.
            exc.outcome_unknown = True
        return _error(exc)
    if data.get("status") in TERMINAL:
        await asyncio.to_thread(runtime.registry.finish_run, context, data["run_id"])
    await asyncio.to_thread(runtime.registry.save_turn, context, _view(data))
    return _view(data)


@router.get("/threads/{thread_id}/runs/{run_id}")
async def get_run(thread_id: UUID, run_id: str, runtime: Runtime, owner: Owner):
    context = await _context(runtime, owner, thread_id)
    try:
        data = _view(await _poll(runtime, context, run_id))
        data["result_references"] = await _references(runtime, context, run_id)
        return data
    except HermesRuntimeError as exc:
        return _error(exc)


@router.post("/threads/{thread_id}/runs/{run_id}/stop")
async def stop_run(thread_id: UUID, run_id: str, body: EmptyInput, runtime: Runtime, owner: Owner):
    context = await _context(runtime, owner, thread_id)
    try:
        data = await runtime.stop_run(context, run_id)
    except RunNotFoundError:
        raise HTTPException(404, "Run was not found") from None
    except HermesRuntimeError as exc:
        return _error(exc)
    if data.get("status") in TERMINAL:
        await asyncio.to_thread(runtime.registry.finish_run, context, run_id)
    await asyncio.to_thread(runtime.registry.save_turn, context, _view(data))
    return _view(data) | {
        "cancellation_scope": "conversation_only",
        "business_tasks_cancelled": False,
    }


@router.get("/threads/{thread_id}/runs/{run_id}/results")
async def list_results(thread_id: UUID, run_id: str, runtime: Runtime, owner: Owner):
    context = await _context(runtime, owner, thread_id)
    try:
        await asyncio.to_thread(runtime.registry.require, context, run_id)
    except RunNotFoundError:
        raise HTTPException(404, "Run was not found") from None
    store = _result_store(runtime)
    items = await asyncio.to_thread(store.list, context.scope_key, run_id)
    for item in items:
        if item["operation"] == "get_scat_track_points":
            item["trajectory_view_available"] = True
    return {"items": items}


@router.get("/threads/{thread_id}/runs/{run_id}/results/{result_id}")
async def get_result(thread_id: UUID, run_id: str, result_id: str, runtime: Runtime, owner: Owner):
    context = await _context(runtime, owner, thread_id)
    try:
        await asyncio.to_thread(runtime.registry.require, context, run_id)
        store = _result_store(runtime)
        from application.readonly_display import receipt_display

        receipt = await asyncio.to_thread(store.get, context.scope_key, run_id, result_id)
        return await asyncio.to_thread(receipt_display, receipt)
    except (RunNotFoundError, ResultNotFoundError):
        raise HTTPException(404, "Result was not found") from None
    except ValueError:
        raise HTTPException(503, "Result is unavailable") from None


@router.get("/threads/{thread_id}/runs/{run_id}/results/{result_id}/trajectory")
async def get_trajectory_view(
    thread_id: UUID, run_id: str, result_id: str, runtime: Runtime, owner: Owner
):
    from application.readonly_extensions import build_extended_business_application
    from application.trajectory_view import trajectory_view

    context = await _context(runtime, owner, thread_id)
    try:
        await asyncio.to_thread(runtime.registry.require, context, run_id)
        # Source verification precedes opening the business application.
        store = _result_store(runtime)
        await asyncio.to_thread(store.get, context.scope_key, run_id, result_id)
        app = getattr(runtime, "trajectory_view_application", None)
        if app is None:
            app = build_extended_business_application()
            runtime.trajectory_view_application = app
        return await trajectory_view(store, context, run_id, result_id, app)
    except (RunNotFoundError, ResultNotFoundError):
        raise HTTPException(404, "Result was not found") from None
    except ValueError:
        raise HTTPException(409, "Trajectory view is unavailable") from None


@router.get("/threads/{thread_id}/runs/{run_id}/events")
async def stream_events(
    thread_id: UUID,
    run_id: str,
    runtime: Runtime,
    owner: Owner,
    last_event_id: Annotated[int | None, Header(ge=0, le=2**63 - 1)] = None,
):
    context = await _context(runtime, owner, thread_id)
    try:
        await _poll(runtime, context, run_id)
    except HermesRuntimeError as exc:
        return _error(exc)

    async def generate():
        try:
            async with aclosing(
                runtime.events(
                    context,
                    run_id,
                    last_sequence=last_event_id if last_event_id is not None else -1,
                )
            ) as events:
                async for event in events:
                    if event.name not in EVENT_FIELDS:
                        continue  # Reasoning and unknown native fields are never forwarded.
                    if event.name == "business.result":
                        if runtime.results is None:
                            continue
                        try:
                            record = await asyncio.to_thread(
                                runtime.results.get,
                                context.scope_key,
                                run_id,
                                event.payload.get("result_id"),
                            )
                        except (ResultNotFoundError, ValueError):
                            continue
                        # Native fields never grant access; the scoped persisted receipt is authoritative.
                        data = {
                            key: value
                            for key, value in record.items()
                            if key in EVENT_FIELDS[event.name]
                        }
                    else:
                        data = {
                            key: value
                            for key, value in event.payload.items()
                            if key in EVENT_FIELDS[event.name]
                        }
                    data.update(event=event.name, run_id=event.run_id)
                    if event.name == "message.delta":
                        await asyncio.to_thread(
                            runtime.registry.save_delta,
                            context,
                            run_id,
                            event.sequence,
                            data.get("delta"),
                        )
                    if event.name.removeprefix("run.") in TERMINAL:
                        await asyncio.to_thread(
                            runtime.registry.save_turn,
                            context,
                            data | {"status": event.name.removeprefix("run.")},
                        )
                        await asyncio.to_thread(runtime.registry.finish_run, context, run_id)
                    prefix = f"id: {event.sequence}\n" if event.sequence is not None else ""
                    yield prefix + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        except HermesRuntimeError as exc:
            yield (
                "data: "
                + json.dumps(
                    {
                        "event": "stream.error",
                        "run_id": run_id,
                        "code": exc.code,
                        "recoverable": True,
                    }
                )
                + "\n\n"
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
