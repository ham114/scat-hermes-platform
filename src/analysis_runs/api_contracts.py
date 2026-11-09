"""Public REST contracts for persisted analysis artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from analysis_runs.artifact_contracts import ARTIFACT_ID_PATTERN, AnalysisArtifactRecord
from schema.analysis import AnalysisResult


class AnalysisArtifactView(BaseModel):
    """Owner-safe artifact representation; internal ownership fields stay private."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    artifact_id: str = Field(min_length=8, max_length=200, pattern=ARTIFACT_ID_PATTERN)
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal["analysis_result"] = "analysis_result"
    thread_id: str = Field(min_length=1, max_length=200)
    model_key: str
    model_version: str
    analysis_result: AnalysisResult
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_record(cls, record: AnalysisArtifactRecord) -> AnalysisArtifactView:
        return cls(
            artifact_id=record.artifact_id,
            run_id=record.run_id,
            thread_id=record.thread_id,
            model_key=record.model_key,
            model_version=record.model_version,
            analysis_result=record.analysis_result,
            created_at=record.created_at,
            expires_at=record.expires_at,
        )


class AnalysisArtifactSummary(BaseModel):
    """Bounded owner-safe metadata used by result discovery pages."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=8, max_length=200, pattern=ARTIFACT_ID_PATTERN)
    model_key: str
    model_version: str
    dataset: str
    operation: str
    title: str
    batch: str | None = None
    external_id: str | None = None
    snapshot_count: int | None = Field(default=None, ge=0)
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_record(cls, record: AnalysisArtifactRecord) -> AnalysisArtifactSummary:
        context = record.analysis_result.context
        raw_snapshot_count = context.get("visualization_snapshot_count")
        snapshot_count = (
            int(raw_snapshot_count)
            if isinstance(raw_snapshot_count, int | float) and raw_snapshot_count >= 0
            else None
        )
        return cls(
            artifact_id=record.artifact_id,
            model_key=record.model_key,
            model_version=record.model_version,
            dataset=record.analysis_result.dataset,
            operation=record.analysis_result.operation,
            title=record.analysis_result.title,
            batch=str(context["batch"]) if context.get("batch") is not None else None,
            external_id=(
                str(context["external_id"]) if context.get("external_id") is not None else None
            ),
            snapshot_count=snapshot_count,
            created_at=record.created_at,
            expires_at=record.expires_at,
        )


class AnalysisArtifactSummaryPage(BaseModel):
    """Recent artifact summaries in one owner scope."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    artifacts: list[AnalysisArtifactSummary]


__all__ = [
    "AnalysisArtifactSummary",
    "AnalysisArtifactSummaryPage",
    "AnalysisArtifactView",
]
