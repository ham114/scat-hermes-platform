"""Deterministic evaluator interface and serializable evaluation result."""

from __future__ import annotations

import math
from typing import Protocol

from pydantic import Field, field_validator

from scat_model_runtime.adapters import ModelInferenceOutput
from scat_model_runtime.component_registry import ComponentRegistry
from scat_model_runtime.contracts import ModelContract
from scat_model_runtime.pipelines import PreparedModelInput

type EvaluationDetailValue = str | int | float | bool | None


class ModelEvaluation(ModelContract):
    metrics: dict[str, float] = Field(default_factory=dict, max_length=100)
    sample_count: int = Field(ge=1, le=1_000_000)
    details: dict[str, EvaluationDetailValue] = Field(default_factory=dict, max_length=100)

    @field_validator("metrics")
    @classmethod
    def require_finite_metrics(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not key or len(key) > 100 for key in value):
            raise ValueError("metric keys must contain 1-100 characters")
        if any(not math.isfinite(metric) for metric in value.values()):
            raise ValueError("evaluation metrics must be finite")
        return value


class Evaluator(Protocol):
    """Calculate metrics from registered model output using deterministic code."""

    def evaluate(
        self,
        *,
        prediction: ModelInferenceOutput,
        expected: PreparedModelInput | None = None,
    ) -> ModelEvaluation: ...


evaluator_registry = ComponentRegistry[Evaluator]("evaluator")


__all__ = ["Evaluator", "ModelEvaluation", "evaluator_registry"]
