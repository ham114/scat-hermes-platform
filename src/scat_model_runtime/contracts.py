"""Strict contracts shared by the internal model-agent and SCAT model runtime."""

from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type ModelParameterScalar = str | int | float | bool | None
type ModelParameterValue = ModelParameterScalar | list[ModelParameterScalar]
_REFERENCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


class ModelContract(BaseModel):
    """Base contract that rejects accidental execution-surface expansion."""

    model_config = ConfigDict(extra="forbid")


class DatasetKind(StrEnum):
    SCAT = "scat"
    UAV = "uav"


class ModelTaskType(StrEnum):
    TRAJECTORY_PREDICTION = "trajectory_prediction"
    ANOMALY_CLASSIFICATION = "anomaly_classification"
    FLIGHT_PHASE_CLASSIFICATION = "flight_phase_classification"
    MODEL_EVALUATION = "model_evaluation"
    MODEL_COMPARISON = "model_comparison"
    DATA_MATERIALIZATION = "data_materialization"
    EXPERIMENT = "experiment"
    REPORT_GENERATION = "report_generation"


class ModelExecutionMode(StrEnum):
    SYNC = "sync"
    ASYNC = "async"


class ModelAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ModelRunStatus(StrEnum):
    CREATED = "created"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    QUEUED = "queued"
    PREPARING_DATA = "preparing_data"
    LOADING_MODEL = "loading_model"
    RUNNING = "running"
    EVALUATING = "evaluating"
    PERSISTING = "persisting"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelResourceLimits(ModelContract):
    """Hard limits declared before a model can be made runnable."""

    max_input_points: int = Field(ge=1, le=1_000_000)
    min_input_points: int = Field(default=1, ge=1, le=1_000_000)
    max_output_points: int = Field(ge=1, le=100_000)
    timeout_seconds: float = Field(gt=0, le=86_400)
    max_batch_size: int = Field(default=1, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_input_bounds(self) -> ModelResourceLimits:
        if self.min_input_points > self.max_input_points:
            raise ValueError("min_input_points cannot exceed max_input_points")
        return self


class ComponentReference(ModelContract):
    """Versioned reference to a registered deterministic runtime component."""

    key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )

    @property
    def registry_id(self) -> str:
        return f"{self.key}:{self.version}"


class ModelSpec(ModelContract):
    """One immutable, versioned task-model registration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    task_type: ModelTaskType
    supported_datasets: frozenset[DatasetKind] = Field(min_length=1)
    input_schema: str = Field(min_length=1, max_length=120)
    output_schema: str = Field(min_length=1, max_length=120)
    input_resolver: ComponentReference | None = None
    feature_pipeline: ComponentReference | None = None
    model_package: ComponentReference | None = None
    execution_backend: ComponentReference | None = None
    execution_release: str | None = Field(default=None, max_length=200)
    evaluator: ComponentReference | None = None
    allowed_evaluators: tuple[ComponentReference, ...] = Field(
        default_factory=tuple,
        max_length=20,
    )
    execution_mode: ModelExecutionMode
    resource_limits: ModelResourceLimits
    availability: ModelAvailability = ModelAvailability.UNAVAILABLE
    unavailable_reason: str | None = Field(default=None, max_length=300)
    is_default: bool = False

    @model_validator(mode="after")
    def validate_availability_reason(self) -> ModelSpec:
        if self.execution_backend is not None and self.execution_backend.version == "latest":
            raise ValueError("execution backend version must be immutable")
        if (
            self.execution_release is not None
            and self.execution_release.rsplit(":", 1)[-1] == "latest"
        ):
            raise ValueError("execution release must be immutable")
        if self.availability == ModelAvailability.UNAVAILABLE:
            if not self.unavailable_reason:
                raise ValueError("unavailable models require unavailable_reason")
        elif self.unavailable_reason is not None:
            raise ValueError("available models cannot have unavailable_reason")
        evaluator_ids = [item.registry_id for item in self.allowed_evaluators]
        if len(evaluator_ids) != len(set(evaluator_ids)):
            raise ValueError("allowed_evaluators must be unique")
        return self

    @property
    def registry_id(self) -> str:
        return f"{self.model_key}:{self.version}"


class ModelInputReference(ModelContract):
    """A bounded business-object reference; never an arbitrary database query."""

    dataset: DatasetKind
    external_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    batch: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    start_time: datetime | None = None
    end_time: datetime | None = None
    start: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=10_000)
    every_nth: int = Field(default=1, ge=1, le=100)
    fields: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    channels: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    @field_validator("start_time", "end_time")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("model input times must include a timezone")
        return value

    @field_validator("fields", "channels")
    @classmethod
    def require_unique_nonempty_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_REFERENCE_KEY_PATTERN.fullmatch(item) is None for item in value):
            raise ValueError("model input field/channel keys are invalid")
        if len(value) != len(set(value)):
            raise ValueError("model input field/channel keys must be unique")
        return value

    @model_validator(mode="after")
    def validate_dataset_reference(self) -> ModelInputReference:
        if self.dataset == DatasetKind.SCAT and self.batch is None:
            raise ValueError("SCAT model input requires batch")
        if self.dataset == DatasetKind.UAV and self.batch is not None:
            raise ValueError("UAV model input does not accept batch")
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time >= self.end_time
        ):
            raise ValueError("model input start_time must be earlier than end_time")
        return self


class ModelTaskRequest(ModelContract):
    """Typed request sent from scat-flight-agent to the internal model-agent."""

    task_type: ModelTaskType
    model_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    model_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )
    # Single requests keep the original JSON shape. Batch callers use input_refs.
    # Exactly one form is accepted, which prevents an ambiguous execution target.
    input_ref: ModelInputReference | None = None
    input_refs: tuple[ModelInputReference, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    parameters: dict[str, ModelParameterValue] = Field(
        default_factory=dict,
        max_length=50,
    )
    evaluator: ComponentReference | None = None
    thread_id: str | None = Field(default=None, max_length=200)
    requested_by: str | None = Field(default=None, max_length=200)
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_input_references(self) -> ModelTaskRequest:
        if (self.input_ref is None) == (not self.input_refs):
            raise ValueError("provide exactly one of input_ref or input_refs")
        references = self.references
        if len({item.model_dump_json() for item in references}) != len(references):
            raise ValueError("model input references must be unique")
        if len({item.dataset for item in references}) != 1:
            raise ValueError("one model task cannot mix datasets")
        return self

    @property
    def references(self) -> tuple[ModelInputReference, ...]:
        if self.input_ref is not None:
            return (self.input_ref,)
        return self.input_refs

    @property
    def primary_input_ref(self) -> ModelInputReference:
        return self.references[0]

    @field_validator("parameters")
    @classmethod
    def bound_parameter_values(
        cls,
        value: dict[str, ModelParameterValue],
    ) -> dict[str, ModelParameterValue]:
        for key, item in value.items():
            if not key or len(key) > 100:
                raise ValueError("model parameter keys must contain 1-100 characters")
            if isinstance(item, list) and len(item) > 100:
                raise ValueError("model parameter lists cannot exceed 100 items")
            values = item if isinstance(item, list) else [item]
            if any(isinstance(part, str) and len(part) > 500 for part in values):
                raise ValueError("model parameter strings cannot exceed 500 characters")
            if any(isinstance(part, float) and not math.isfinite(part) for part in values):
                raise ValueError("model parameter numbers must be finite")
        return value


class ModelTaskReceipt(ModelContract):
    """Minimal stable response before run/artifact persistence is implemented."""

    run_id: str = Field(min_length=1, max_length=100)
    status: ModelRunStatus
    progress: float = Field(default=0, ge=0, le=1)
    message: str | None = Field(default=None, max_length=500)


__all__ = [
    "ComponentReference",
    "DatasetKind",
    "ModelAvailability",
    "ModelContract",
    "ModelExecutionMode",
    "ModelInputReference",
    "ModelParameterScalar",
    "ModelParameterValue",
    "ModelResourceLimits",
    "ModelRunStatus",
    "ModelSpec",
    "ModelTaskReceipt",
    "ModelTaskRequest",
    "ModelTaskType",
]
