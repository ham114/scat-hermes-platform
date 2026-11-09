"""Framework-neutral task model adapter interface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import Field

from scat_model_runtime.component_registry import ComponentRegistry
from scat_model_runtime.contracts import ModelParameterValue
from scat_model_runtime.pipelines import PreparedModelInput, RuntimePayloadContract


class ModelInferenceOutput(RuntimePayloadContract):
    output_schema: str = Field(min_length=1, max_length=120)
    point_count: int = Field(ge=1, le=100_000)
    batch_size: int = Field(default=1, ge=1, le=10_000)
    payload: object


class ModelAdapter(Protocol):
    """Load and execute one registered model version without interpreting text."""

    def load(self) -> None: ...

    def predict(
        self,
        *,
        model_input: PreparedModelInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ModelInferenceOutput: ...

    def close(self) -> None: ...


model_adapter_registry = ComponentRegistry[ModelAdapter]("model adapter")


__all__ = [
    "ModelAdapter",
    "ModelInferenceOutput",
    "model_adapter_registry",
]
