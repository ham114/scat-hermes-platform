"""Feature pipeline interface and framework-neutral data envelopes."""

from __future__ import annotations

from typing import Protocol

from pydantic import ConfigDict, Field, field_validator

from scat_model_runtime.component_registry import ComponentRegistry
from scat_model_runtime.contracts import ModelContract, ModelInputReference, ModelTaskRequest


class RuntimePayloadContract(ModelContract):
    """Internal envelope; payload never crosses the Agent or persistence boundary."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class ResolvedModelInput(RuntimePayloadContract):
    input_ref: ModelInputReference
    point_count: int = Field(ge=1, le=1_000_000)
    batch_size: int = Field(default=1, ge=1, le=10_000)
    payload: object


class PreparedModelInput(RuntimePayloadContract):
    input_schema: str = Field(min_length=1, max_length=120)
    feature_names: tuple[str, ...] = Field(min_length=1, max_length=512)
    point_count: int = Field(ge=1, le=1_000_000)
    batch_size: int = Field(default=1, ge=1, le=10_000)
    payload: object

    @field_validator("feature_names")
    @classmethod
    def require_unique_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not feature or len(feature) > 100 for feature in value):
            raise ValueError("feature names must contain 1-100 characters")
        if len(value) != len(set(value)):
            raise ValueError("feature names must be unique")
        return value


class FeaturePipeline(Protocol):
    """Deterministically transform resolved business data into model input."""

    def prepare(
        self,
        *,
        source: ResolvedModelInput,
        request: ModelTaskRequest,
    ) -> PreparedModelInput: ...


class ModelInputResolver(Protocol):
    """Resolve one bounded business reference through an application service."""

    async def resolve(self, *, request: ModelTaskRequest) -> ResolvedModelInput: ...


input_resolver_registry = ComponentRegistry[ModelInputResolver]("model input resolver")
feature_pipeline_registry = ComponentRegistry[FeaturePipeline]("feature pipeline")


__all__ = [
    "FeaturePipeline",
    "ModelInputResolver",
    "PreparedModelInput",
    "ResolvedModelInput",
    "feature_pipeline_registry",
    "input_resolver_registry",
]
