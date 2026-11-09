"""Authenticated current-user memory control; no caller supplied identity or credentials."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from service.auth import verify_bearer
from service.hermes_api import Owner

router = APIRouter(
    prefix="/api/v1/hermes/memory", tags=["Memory"], dependencies=[Depends(verify_bearer)]
)


def runtime(request: Request):
    value = getattr(request.app.state, "memory_runtime", None)
    if value is None:
        raise HTTPException(503, "memory_not_configured")
    return value


Memory = Annotated[object, Depends(runtime)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    content: str = Field(min_length=1, max_length=2000)


@router.get("")
def status(owner: Owner, memory: Memory):
    return memory.status(owner)


@router.get("/context")
async def context(owner: Owner, memory: Memory):
    return await memory.context(owner)


@router.get("/corrections")
def correction_records(owner: Owner, memory: Memory):
    return memory.corrections(owner)


@router.get("/learning")
def learning(owner: Owner):
    from pathlib import Path

    from hermes_runtime.autonomy_config import load_autonomy
    from hermes_runtime.experience_learning import LearningLedger

    root = Path(__file__).resolve().parents[2]
    policy = load_autonomy(root)
    if policy is None:
        raise HTTPException(503, "learning_not_configured")
    return LearningLedger(root, policy.learning, read_only=True).status(owner)


@router.put("/settings")
def settings(body: Settings, owner: Owner, memory: Memory):
    return memory.settings(owner, body.enabled)


@router.post("/corrections", status_code=202)
def corrections(body: Correction, owner: Owner, memory: Memory):
    try:
        return memory.correct(owner, body.content, body.request_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("", status_code=202)
def forget(owner: Owner, memory: Memory):
    return memory.forget(owner)
