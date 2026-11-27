"""Versioned visualization requests and independent review results."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schema.analysis import AnalysisResult

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Viewport = Literal["desktop", "mobile"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SourceReference(Contract):
    kind: Literal["readonly_result", "analysis_artifact"]
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,132}$")
    payload_sha256: Digest


class ExecutionLimits(Contract):
    max_provider_requests: int = Field(default=10, ge=1, le=30)
    max_total_tokens: int = Field(default=60000, ge=1, le=500000)
    max_revisions: int = Field(default=2, ge=0, le=5)
    wall_seconds: int = Field(default=900, ge=1, le=3600)
    daily_provider_requests: int = Field(default=100, ge=1, le=10000)
    daily_tokens: int = Field(default=600000, ge=1, le=5000000)


class VisualizationRequest(Contract):
    """Trusted snapshot references, not arbitrary filesystem paths or raw credentials."""

    schema_version: Literal[1] = 1
    intent: str = Field(min_length=1, max_length=8000)
    snapshot_id: Identity
    snapshot_sha256: Digest
    preferences_sha256: Digest
    preferences: dict[str, str | int | float | bool | None] | None = Field(
        default=None, max_length=32
    )
    viewports: tuple[Viewport, ...] = Field(
        default=("desktop", "mobile"), min_length=1, max_length=2
    )
    parent_artifact_id: Identity | None = None
    parent_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def consistent_references(self):
        if self.preferences is not None:
            raw = json.dumps(
                self.preferences,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            if hashlib.sha256(raw.encode()).hexdigest() != self.preferences_sha256:
                raise ValueError("preferences digest mismatch")
        if not self.intent.strip() or len(set(self.viewports)) != len(self.viewports):
            raise ValueError("nonempty intent and distinct viewports required")
        if (self.parent_artifact_id is None) != (self.parent_version is None):
            raise ValueError("parent artifact and version must be supplied together")
        return self


class CreateVisualization(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    intent: str = Field(min_length=1, max_length=8000)
    data_refs: tuple[SourceReference, ...] = Field(min_length=1, max_length=16)


class VisualizationSourceView(SourceReference):
    operation: str


class VisualizationSourceList(Contract):
    items: list[VisualizationSourceView]


class ReviseVisualization(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    feedback: str = Field(min_length=1, max_length=4000)


class VisualizationTaskView(Contract):
    task_id: Identity
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "needs_reconciliation"]
    phase: str
    intent: str
    created_at: float
    error_code: str | None = None
    artifact_id: Identity | None = None
    parent_artifact_id: Identity | None = None
    version: int = Field(ge=1)


class VisualizationTaskList(Contract):
    enabled: bool
    items: list[VisualizationTaskView]


class VisualizationArtifactView(Contract):
    schema_version: Literal[1] = 1
    artifact_id: Identity
    task_id: Identity
    version: int = Field(ge=1)
    parent_artifact_id: Identity | None = None
    intent: str
    created_at: float
    snapshot_sha256: Digest
    source_sha256: Digest
    review_id: Identity
    viewports: tuple[Viewport, ...]
    data_refs: tuple[SourceReference, ...]
    files: dict[str, Digest]
    analysis_result: AnalysisResult


class ReviewIssue(Contract):
    severity: Literal["blocker", "major", "minor", "info"]
    location: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=1000)
    suggestion: str = Field(min_length=1, max_length=1000)


class ReviewResult(Contract):
    decision: Literal["pass", "revise", "reject"]
    issues: tuple[ReviewIssue, ...] = Field(default=(), max_length=30)
    checked_viewports: tuple[Viewport, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def consistent_decision(self):
        if len(set(self.checked_viewports)) != len(self.checked_viewports):
            raise ValueError("duplicate reviewed viewport")
        if self.decision == "pass" and any(
            issue.severity in {"blocker", "major"} for issue in self.issues
        ):
            raise ValueError("major or blocker issues cannot pass review")
        if self.decision != "pass" and not self.issues:
            raise ValueError("revision or rejection requires actionable issues")
        return self
