"""Authenticated conversation delivery of independently reviewed static charts."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from service.auth import verify_bearer
from service.hermes_api import Owner, Runtime, _context
from visualization_runtime.contracts import (
    CreateVisualization,
    ReviseVisualization,
    SourceReference,
    VisualizationArtifactView,
    VisualizationSourceList,
    VisualizationTaskList,
    VisualizationTaskView,
)
from visualization_runtime.snapshots import SnapshotUnavailable
from visualization_runtime.store import JobConflict, JobNotFound

router = APIRouter(
    prefix="/api/v1/hermes", tags=["Visualizations"], dependencies=[Depends(verify_bearer)]
)
Identity = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


def get_visualizations(request: Request):
    app = getattr(request.app.state, "visualization_tasks", None)
    if app is None:
        raise HTTPException(503, "Visualization service is unavailable")
    return app


Tasks = Annotated[object, Depends(get_visualizations)]


def translate(exc):
    if isinstance(exc, (JobNotFound, SnapshotUnavailable, LookupError)):
        return HTTPException(404, "Visualization or source was not found")
    return HTTPException(
        409, "Visualization request cannot proceed; inspect the existing task and data scope"
    )


@router.post(
    "/threads/{thread_id}/visualizations", status_code=202, response_model=VisualizationTaskView
)
async def create(
    thread_id: UUID, body: CreateVisualization, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        for ref in body.data_refs:
            if ref.kind == "readonly_result":
                await asyncio.to_thread(runtime.registry.require, context, ref.run_id)
        runs = await asyncio.to_thread(runtime.registry.list_runs, context, 1)
        if not runs:
            raise JobConflict("visualization_parent_budget_unavailable")
        row = await tasks.create(context, body, parent_run_id=runs[0]["run_id"], learn=True)
        return await asyncio.to_thread(tasks.view, context, row["task_id"])
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.get("/threads/{thread_id}/visualization-sources", response_model=VisualizationSourceList)
async def sources(thread_id: UUID, runtime: Runtime, owner: Owner, tasks: Tasks):
    context = await _context(runtime, owner, thread_id)
    return await tasks.sources(context, runtime.registry)


@router.get("/threads/{thread_id}/visualization-tasks", response_model=VisualizationTaskList)
async def list_tasks(thread_id: UUID, runtime: Runtime, owner: Owner, tasks: Tasks):
    context = await _context(runtime, owner, thread_id)
    return await asyncio.to_thread(tasks.list, context)


@router.get(
    "/threads/{thread_id}/visualization-tasks/{task_id}", response_model=VisualizationTaskView
)
async def status(thread_id: UUID, task_id: Identity, runtime: Runtime, owner: Owner, tasks: Tasks):
    context = await _context(runtime, owner, thread_id)
    try:
        return await asyncio.to_thread(tasks.view, context, task_id)
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.post(
    "/threads/{thread_id}/visualization-tasks/{task_id}/cancel",
    response_model=VisualizationTaskView,
)
async def cancel(thread_id: UUID, task_id: Identity, runtime: Runtime, owner: Owner, tasks: Tasks):
    context = await _context(runtime, owner, thread_id)
    try:
        await asyncio.to_thread(tasks.jobs.cancel, context, task_id)
        return await asyncio.to_thread(tasks.view, context, task_id)
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.get(
    "/threads/{thread_id}/visualizations/{artifact_id}", response_model=VisualizationArtifactView
)
async def artifact(
    thread_id: UUID, artifact_id: Identity, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        return await asyncio.to_thread(tasks.artifact_view, context, artifact_id)
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.post(
    "/threads/{thread_id}/visualizations/{artifact_id}/revisions",
    status_code=202,
    response_model=VisualizationTaskView,
)
async def revise(
    thread_id: UUID,
    artifact_id: Identity,
    body: ReviseVisualization,
    runtime: Runtime,
    owner: Owner,
    tasks: Tasks,
):
    context = await _context(runtime, owner, thread_id)
    try:
        original = await asyncio.to_thread(tasks.publisher.get, context, artifact_id)
        job = await asyncio.to_thread(tasks.jobs.get, context, original["task_id"])
        with tasks.jobs.transaction() as db:
            row = db.execute(
                "SELECT run_id FROM visualization_parent_allocations WHERE task_id=?",
                (job["task_id"],),
            ).fetchone()
        runs = await asyncio.to_thread(runtime.registry.list_runs, context, 1)
        if row is None and not runs:
            raise JobConflict("visualization_parent_budget_unavailable")
        result = await asyncio.to_thread(
            tasks.revise,
            context,
            artifact_id,
            body,
            parent_run_id=runs[0]["run_id"] if runs else row[0],
            learn=True,
        )
        return await asyncio.to_thread(tasks.view, context, result["task_id"])
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.get("/threads/{thread_id}/visualizations/{artifact_id}/files/{file_id}")
async def file(
    thread_id: UUID,
    artifact_id: Identity,
    file_id: str,
    runtime: Runtime,
    owner: Owner,
    tasks: Tasks,
):
    context = await _context(runtime, owner, thread_id)
    if file_id not in {"desktop.png", "mobile.png", "source.js"}:
        raise HTTPException(404, "Visualization file was not found")
    try:
        raw = await asyncio.to_thread(tasks.publisher.read_file, context, artifact_id, file_id)
        return Response(
            raw,
            media_type="image/png" if file_id.endswith(".png") else "text/plain",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Content-Disposition": f'{"inline" if file_id.endswith(".png") else "attachment"}; filename="{file_id}"',
            },
        )
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.post("/threads/{thread_id}/visualization-sources/revoke")
async def revoke_source(
    thread_id: UUID, body: SourceReference, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        if body.kind == "readonly_result":
            await asyncio.to_thread(runtime.registry.require, context, body.run_id)
        # Verify the exact source in this conversation, including expired receipts
        # already materialized into an authorized snapshot.
        with tasks.jobs.transaction() as db:
            rows = db.execute(
                "SELECT s.snapshot_id FROM visualization_snapshot_sources s JOIN visualization_snapshots v USING(snapshot_id) WHERE v.owner=? AND v.thread=? AND s.kind=? AND s.source_id=?",
                (owner, context.thread_id, body.kind, body.source_id),
            ).fetchall()
        if not rows:
            raise JobNotFound("visualization_source_not_found")
        await asyncio.to_thread(tasks.snapshots.revoke_source, context, body.kind, body.source_id)
        return {"revoked": True}
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None


@router.delete("/threads/{thread_id}/runs/{run_id}/results/{result_id}")
async def delete_source(
    thread_id: UUID, run_id: str, result_id: Identity, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        await asyncio.to_thread(runtime.registry.require, context, run_id)
        await asyncio.to_thread(tasks.receipts.get, context.scope_key, run_id, result_id)
        # Revocation commits before deletion. A crash can over-restrict access,
        # but can never leave derived published images readable after deletion.
        await asyncio.to_thread(
            tasks.snapshots.revoke_source, context, "readonly_result", result_id
        )
        await asyncio.to_thread(tasks.receipts.delete, context.scope_key, run_id, result_id)
        return {"deleted": True}
    except (LookupError, ValueError) as exc:
        raise translate(exc) from None
