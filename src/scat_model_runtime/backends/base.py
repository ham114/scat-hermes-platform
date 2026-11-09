"""Common execution semantics independent of model or transport."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

run_identity: ContextVar[str | None] = ContextVar("model_backend_run_id", default=None)


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    model_key: str
    model_version: str
    input_schema: str
    output_schema: str
    payload: dict[str, JsonValue]
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)


class InferenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    request_id: str
    run_id: str
    model_key: str
    model_version: str
    input_schema: str
    output_schema: str
    payload: dict[str, JsonValue]
    provenance: dict[str, JsonValue] = Field(default_factory=dict)

    def verify_identity(self, request: InferenceRequest) -> None:
        for key in (
            "request_id",
            "run_id",
            "model_key",
            "model_version",
            "input_schema",
            "output_schema",
        ):
            if getattr(self, key) != getattr(request, key):
                raise BackendError("invalid_response", execution_state="unknown")


class BackendError(RuntimeError):
    """Safe public failure. Unknown execution must never be automatically retried."""

    def __init__(self, code: str, *, execution_state: str = "not_started", retryable: bool = False):
        super().__init__("Model backend request could not be completed.")
        self.code = code
        self.execution_state = execution_state
        self.retryable = retryable and execution_state == "not_started"


@dataclass(frozen=True)
class BackendHealth:
    ready: bool
    code: str = "ready"
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CancelResult:
    accepted: bool
    execution_state: Literal["not_started", "unknown"]
    remote_termination_confirmed: bool = False


class ExecutionBackend(Protocol):
    def health(self) -> BackendHealth: ...
    def execute(self, request: InferenceRequest) -> InferenceResult: ...
    def cancel(self, request_id: str) -> CancelResult: ...
    def close(self) -> None: ...
