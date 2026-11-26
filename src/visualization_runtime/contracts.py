"""Versioned visualization requests and independent review results."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Viewport = Literal["desktop", "mobile"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class VisualizationRequest(Contract):
    """Trusted snapshot references, not arbitrary filesystem paths or raw credentials."""

    schema_version: Literal[1] = 1
    intent: str = Field(min_length=1, max_length=8000)
    snapshot_id: Identity
    snapshot_sha256: Digest
    preferences_sha256: Digest
    viewports: tuple[Viewport, ...] = Field(
        default=("desktop", "mobile"), min_length=1, max_length=2
    )
    parent_artifact_id: Identity | None = None
    parent_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def consistent_references(self):
        if not self.intent.strip() or len(set(self.viewports)) != len(self.viewports):
            raise ValueError("nonempty intent and distinct viewports required")
        if (self.parent_artifact_id is None) != (self.parent_version is None):
            raise ValueError("parent artifact and version must be supplied together")
        return self


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
