"""Versioned MongoDB contracts for reproducible model runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from scat_model_runtime.contracts import (
    ComponentReference,
    ModelContract,
    ModelExecutionMode,
    ModelInputReference,
    ModelParameterValue,
    ModelRunStatus,
    ModelTaskType,
)


class AnalysisRunStatusEvent(ModelContract):
    """One append-only status change in a model run."""

    status: ModelRunStatus
    progress: float = Field(ge=0, le=1)
    at: datetime
    message: str | None = Field(default=None, max_length=500)

    @field_validator("at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("analysis run timestamps must include a timezone")
        return value


class AnalysisRunComponents(ModelContract):
    """Exact deterministic component versions selected for a run."""

    input_resolver: ComponentReference | None = None
    feature_pipeline: ComponentReference | None = None
    model_package: ComponentReference | None = None
    execution_backend: ComponentReference | None = None
    execution_release: str | None = Field(default=None, max_length=200)
    evaluator: ComponentReference | None = None


class AnalysisRunError(ModelContract):
    """Safe failure details; internal exceptions and paths are never stored here."""

    code: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    message: str = Field(min_length=1, max_length=500)
    request_id: str | None = Field(default=None, max_length=100)


class AnalysisRunRecord(ModelContract):
    """Small durable run record; large output payloads belong in artifacts."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    owner_user_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    task_type: ModelTaskType
    execution_mode: ModelExecutionMode
    model_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    model_version: str = Field(min_length=1, max_length=64)
    input_schema: str = Field(min_length=1, max_length=120)
    output_schema: str = Field(min_length=1, max_length=120)
    input_refs: tuple[ModelInputReference, ...] = Field(min_length=1, max_length=100)
    parameters: dict[str, ModelParameterValue] = Field(default_factory=dict, max_length=50)
    components: AnalysisRunComponents
    code_version: str = Field(min_length=1, max_length=120)
    status: ModelRunStatus
    progress: float = Field(ge=0, le=1)
    status_history: tuple[AnalysisRunStatusEvent, ...] = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    result_summary: dict[str, Any] | None = None
    result_artifact_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    result_schema_version: str | None = Field(default=None, max_length=64)
    error: AnalysisRunError | None = None

    @field_validator(
        "created_at",
        "updated_at",
        "queued_at",
        "started_at",
        "finished_at",
        "expires_at",
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("analysis run timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> AnalysisRunRecord:
        if self.status_history[-1].status != self.status:
            raise ValueError("latest status history event must match current status")
        if self.status_history[-1].progress != self.progress:
            raise ValueError("latest status history event must match current progress")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot be earlier than created_at")
        if self.finished_at is not None:
            if self.started_at is None:
                raise ValueError("finished_at requires started_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot be earlier than started_at")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.status == ModelRunStatus.FAILED and self.error is None:
            raise ValueError("failed analysis runs require an error")
        if self.status != ModelRunStatus.FAILED and self.error is not None:
            raise ValueError("only failed analysis runs may contain an error")
        return self

    def to_mongo_document(self) -> dict[str, Any]:
        """Return BSON-compatible values while preserving datetime objects for TTL."""

        payload = self.model_dump(mode="python", exclude_none=True)
        # BSON dates have millisecond precision and may decode without tzinfo.
        # Input windows are exact source timestamps, not TTL fields: retain ISO
        # strings with timezone and microseconds across the persistence boundary.
        payload["input_refs"] = [
            ref.model_dump(mode="json", exclude_none=True) for ref in self.input_refs
        ]
        return payload

    @classmethod
    def from_mongo_document(cls, document: dict[str, Any]) -> AnalysisRunRecord:
        payload = {key: value for key, value in document.items() if key != "_id"}
        # BSON stores UTC datetimes without tzinfo unless the client is globally
        # configured as tz-aware. Normalize only this bounded contract instead
        # of changing timestamp behavior for every existing flight query.
        timestamp_fields = (
            "created_at",
            "updated_at",
            "queued_at",
            "started_at",
            "finished_at",
            "expires_at",
        )
        for field_name in timestamp_fields:
            value = payload.get(field_name)
            if isinstance(value, datetime) and value.tzinfo is None:
                payload[field_name] = value.replace(tzinfo=UTC)
        references = payload.get("input_refs")
        if isinstance(references, list | tuple):
            normalized = []
            for item in references:
                if not isinstance(item, dict):
                    normalized.append(item)
                    continue
                value = dict(item)
                for key in ("start_time", "end_time"):
                    moment = value.get(key)
                    if isinstance(moment, datetime) and moment.tzinfo is None:
                        value[key] = moment.replace(tzinfo=UTC)
                normalized.append(value)
            payload["input_refs"] = normalized
        history = payload.get("status_history")
        if isinstance(history, list | tuple):
            normalized_history: list[Any] = []
            for item in history:
                if not isinstance(item, dict):
                    normalized_history.append(item)
                    continue
                normalized_item = dict(item)
                event_at = normalized_item.get("at")
                if isinstance(event_at, datetime) and event_at.tzinfo is None:
                    normalized_item["at"] = event_at.replace(tzinfo=UTC)
                normalized_history.append(normalized_item)
            payload["status_history"] = normalized_history
        return cls.model_validate(payload)
