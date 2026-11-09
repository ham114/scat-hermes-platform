"""Human confirmation endpoints; none are registered as LLM execution tools."""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from application.model_tasks import ModelTasks, TaskProposal
from application.task_store import TaskConflict, TaskNotFound
from scat_model_runtime import ModelRegistryError
from service.auth import verify_bearer
from service.hermes_api import Owner, Runtime, _context

router = APIRouter(
    prefix="/api/v1/hermes", tags=["Model tasks"], dependencies=[Depends(verify_bearer)]
)
TaskId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


def get_tasks(request: Request) -> ModelTasks:
    tasks = getattr(request.app.state, "model_tasks", None)
    if tasks is None:
        raise HTTPException(503, "Model task service is unavailable")
    return tasks


Tasks = Annotated[ModelTasks, Depends(get_tasks)]


class PrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    proposal: TaskProposal
    budget: dict[str, int] | None = None


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def translate(exc):
    if isinstance(exc, TaskNotFound):
        return HTTPException(404, "Model task or artifact was not found")
    return HTTPException(
        409,
        "Task parameters, registration or confirmation are no longer valid; inspect the current task",
    )


@router.get("/model-catalog")
async def catalog(tasks: Tasks):
    return {"items": await asyncio.to_thread(tasks.catalog)}


@router.post("/threads/{thread_id}/model-tasks", status_code=201)
async def prepare(
    thread_id: UUID, body: PrepareInput, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        options = {"proposal_key": str(body.request_id)}
        if body.budget is not None:
            if tasks.skills is None:
                raise TaskConflict("Skill budget is not available in legacy H5")
            options["budget"] = body.budget
        return await asyncio.to_thread(tasks.prepare, context, body.proposal, **options)
    except (TaskConflict, ModelRegistryError, ValueError) as exc:
        raise translate(exc) from None


@router.get("/threads/{thread_id}/model-tasks")
async def list_tasks(thread_id: UUID, runtime: Runtime, owner: Owner, tasks: Tasks):
    context = await _context(runtime, owner, thread_id)
    rows = await asyncio.to_thread(tasks.store.list, context.scope_key)
    return {"items": [await tasks.get(context, row["task_id"]) for row in rows]}


@router.get("/threads/{thread_id}/model-tasks/{task_id}")
async def get_task(thread_id: UUID, task_id: TaskId, runtime: Runtime, owner: Owner, tasks: Tasks):
    context = await _context(runtime, owner, thread_id)
    try:
        return await tasks.get(context, task_id)
    except (TaskNotFound, TaskConflict) as exc:
        raise translate(exc) from None


@router.post("/threads/{thread_id}/model-tasks/{task_id}/confirm", status_code=202)
async def confirm(
    thread_id: UUID,
    task_id: TaskId,
    body: ConfirmInput,
    runtime: Runtime,
    owner: Owner,
    tasks: Tasks,
):
    context = await _context(runtime, owner, thread_id)
    if body.confirmed is not True:
        raise HTTPException(409, "An explicit confirmation is required")
    try:
        return await asyncio.to_thread(tasks.confirm, context, task_id, body.snapshot_sha256)
    except (TaskNotFound, TaskConflict, ModelRegistryError) as exc:
        raise translate(exc) from None


@router.post("/threads/{thread_id}/model-tasks/{task_id}/revoke")
async def revoke(
    thread_id: UUID, task_id: TaskId, body: EmptyInput, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        row = await asyncio.to_thread(tasks.store.get, context.scope_key, task_id)
        if tasks.skills is not None and "skills" in row["snapshot"] and row["state"] != "pending":
            tasks.skills.store.cancel(context.scope_key, task_id)
            tasks.skills.reconcile(context, task_id)
            return await tasks.get(context, task_id)
        return tasks.view(await asyncio.to_thread(tasks.store.revoke, context.scope_key, task_id))
    except (TaskNotFound, TaskConflict) as exc:
        raise translate(exc) from None


class BudgetRevisionInput(ConfirmInput):
    limits: dict[str, int]


@router.post("/threads/{thread_id}/model-tasks/{task_id}/cancel")
async def cancel_job(
    thread_id: UUID, task_id: TaskId, body: EmptyInput, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        if tasks.skills is None:
            raise TaskConflict("Skill jobs unavailable")
        await asyncio.to_thread(tasks.skills.store.cancel, context.scope_key, task_id)
        await asyncio.to_thread(tasks.skills.reconcile, context, task_id)
        return await tasks.get(context, task_id)
    except (TaskNotFound, TaskConflict) as exc:
        raise translate(exc) from None


@router.post("/threads/{thread_id}/model-tasks/{task_id}/reconcile")
async def reconcile_job(
    thread_id: UUID, task_id: TaskId, body: EmptyInput, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    try:
        if tasks.skills is None:
            raise TaskConflict("Skill jobs unavailable")
        row = await asyncio.to_thread(tasks.skills.store.get, context.scope_key, task_id)
        from application.skill_worker import completion_reference, publish_result, result_outcome

        reference = completion_reference(tasks, row)
        if reference and row["job"]["state"] in {"unknown", "paused"}:
            await publish_result(tasks, row, reference)
            tasks.skills.store.state(
                context.scope_key,
                task_id,
                result_outcome(tasks, row, reference),
                reference=reference,
            )
            from hermes_runtime.job_budget import ensure_account

            ensure_account(tasks.skills.store, row).finish()
        else:
            await asyncio.to_thread(tasks.skills.reconcile, context, task_id)
        return await tasks.get(context, task_id)
    except (TaskNotFound, TaskConflict, ValueError) as exc:
        raise translate(exc) from None


@router.post("/threads/{thread_id}/model-tasks/{task_id}/budget")
async def revise_job_budget(
    thread_id: UUID,
    task_id: TaskId,
    body: BudgetRevisionInput,
    runtime: Runtime,
    owner: Owner,
    tasks: Tasks,
):
    context = await _context(runtime, owner, thread_id)
    try:
        if tasks.skills is None or body.confirmed is not True:
            raise TaskConflict("Explicit budget approval required")
        row = tasks.skills.store.get(context.scope_key, task_id)
        if row["sha256"] != body.snapshot_sha256:
            raise TaskConflict("Approved snapshot changed")
        await asyncio.to_thread(tasks.skills.revise_budget, context, task_id, body.limits)
        return await tasks.get(context, task_id)
    except (TaskNotFound, TaskConflict, ValueError) as exc:
        raise translate(exc) from None


@router.get("/threads/{thread_id}/model-tasks/{task_id}/saved-model-output")
async def saved_output(thread_id: UUID, task_id: TaskId, runtime: Runtime, owner: Owner, tasks: Tasks):
    from application.model_output_view import saved_model_output

    context = await _context(runtime, owner, thread_id)
    try:
        return await asyncio.to_thread(saved_model_output, context, tasks, task_id)
    except (TaskNotFound, TaskConflict) as exc:
        raise translate(exc) from None
    except ValueError:
        raise HTTPException(503, "Saved model output is unavailable") from None


@router.get("/threads/{thread_id}/model-tasks/{task_id}/artifacts/{artifact_id}")
async def artifact(
    thread_id: UUID, task_id: TaskId, artifact_id: str, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    if not 8 <= len(artifact_id) <= 200:
        raise HTTPException(422, "Invalid artifact ID")
    try:
        return await tasks.artifact(context, task_id, artifact_id)
    except (TaskNotFound, TaskConflict) as exc:
        raise translate(exc) from None
