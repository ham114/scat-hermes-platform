"""Workflow preparation and capability discovery; existing task endpoints own controls."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from application.task_store import TaskConflict
from service.auth import verify_bearer
from service.hermes_api import Owner, Runtime, _context
from service.model_task_api import Tasks, translate
from workflow_runtime.catalog import describe
from workflow_runtime.contracts import WorkflowProposal

router = APIRouter(
    prefix="/api/v1/hermes", tags=["Workflow tasks"], dependencies=[Depends(verify_bearer)]
)


class PrepareWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    proposal: WorkflowProposal
    budget: dict[str, int] | None = None


@router.get("/workflow-catalog")
async def catalog(tasks: Tasks):
    return {
        **describe(),
        "enabled": bool(getattr(tasks, "workflow_enabled", False)),
        "synthetic": bool(getattr(tasks, "workflow_source", None)),
    }


@router.post("/threads/{thread_id}/workflow-tasks", status_code=201)
async def prepare(
    thread_id: UUID, body: PrepareWorkflow, runtime: Runtime, owner: Owner, tasks: Tasks
):
    context = await _context(runtime, owner, thread_id)
    if not getattr(tasks, "workflow_enabled", False):
        raise HTTPException(409, "Workflow profile is not enabled")
    try:
        return await tasks.prepare_workflow(
            context, body.proposal, proposal_key=str(body.request_id), budget=body.budget
        )
    except (TaskConflict, ValueError) as exc:
        raise translate(exc) from None
