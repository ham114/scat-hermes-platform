"""Tests for framework-neutral model runtime component boundaries."""

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from scat_model_runtime import (
    ComponentReference,
    ComponentRegistration,
    ComponentRegistry,
    ComponentVersionRequiredError,
    DatasetKind,
    DuplicateComponentError,
    ModelEvaluation,
    ModelInferenceOutput,
    ModelInputReference,
    ModelParameterValue,
    ModelTaskRequest,
    ModelTaskType,
    PreparedModelInput,
    ResolvedModelInput,
)
from scat_model_runtime.component_registry import ComponentNotFoundError


class _TestPipeline:
    def prepare(
        self,
        *,
        source: ResolvedModelInput,
        request: ModelTaskRequest,
    ) -> PreparedModelInput:
        assert request.input_ref == source.input_ref
        return PreparedModelInput(
            input_schema="test-features/v1",
            feature_names=("x", "y"),
            point_count=source.point_count,
            batch_size=source.batch_size,
            payload=source.payload,
        )


class _TestAdapter:
    def load(self) -> None:
        pass

    def predict(
        self,
        *,
        model_input: PreparedModelInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ModelInferenceOutput:
        scale = float(parameters.get("scale", 1.0))
        values = tuple(value * scale for value in model_input.payload)
        return ModelInferenceOutput(
            output_schema="test-prediction/v1",
            point_count=len(values),
            payload=values,
        )

    def close(self) -> None:
        pass


class _TestEvaluator:
    def evaluate(
        self,
        *,
        prediction: ModelInferenceOutput,
        expected: PreparedModelInput | None = None,
    ) -> ModelEvaluation:
        assert expected is not None
        differences = [
            abs(actual - predicted)
            for actual, predicted in zip(expected.payload, prediction.payload, strict=True)
        ]
        return ModelEvaluation(
            metrics={"mae": sum(differences) / len(differences)},
            sample_count=len(differences),
        )


def _ref(version: str = "1.0.0") -> ComponentReference:
    return ComponentReference(key="test-component", version=version)


def test_component_registry_is_versioned_and_explicit():
    registry = ComponentRegistry[object]("test component")
    component = object()
    registry.register(ComponentRegistration(_ref(), component, is_default=True))

    assert registry.resolve(_ref()) is component
    assert registry.get("test-component").component is component
    with pytest.raises(DuplicateComponentError, match="already registered"):
        registry.register(ComponentRegistration(_ref(), object()))
    with pytest.raises(ComponentNotFoundError, match="not registered"):
        registry.resolve(ComponentReference(key="missing", version="1.0.0"))


def test_component_registry_requires_a_version_without_default():
    registry = ComponentRegistry[object]("test component")
    registry.register(ComponentRegistration(_ref(), object()))
    registry.register(ComponentRegistration(_ref("2.0.0"), object()))

    with pytest.raises(ComponentVersionRequiredError, match="version is required"):
        registry.get("test-component")


def test_test_only_components_share_framework_neutral_envelopes():
    input_ref = ModelInputReference(
        dataset=DatasetKind.UAV,
        external_id="flight-1",
        channels=("x", "y"),
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        model_key="test-model",
        input_ref=input_ref,
        parameters={"scale": 2.0},
        confirmed=True,
    )
    resolved = ResolvedModelInput(
        input_ref=input_ref,
        point_count=2,
        payload=(1.0, 2.0),
    )

    prepared = _TestPipeline().prepare(source=resolved, request=request)
    prediction = _TestAdapter().predict(
        model_input=prepared,
        parameters=request.parameters,
    )
    evaluation = _TestEvaluator().evaluate(
        prediction=prediction,
        expected=prepared,
    )

    assert prediction.payload == (2.0, 4.0)
    assert evaluation.metrics == {"mae": 1.5}


def test_component_envelopes_reject_invalid_shapes_and_metrics():
    with pytest.raises(ValidationError, match="feature names must be unique"):
        PreparedModelInput(
            input_schema="test/v1",
            feature_names=("x", "x"),
            point_count=2,
            payload=(1.0, 2.0),
        )
    with pytest.raises(ValidationError, match="must be finite"):
        ModelEvaluation(metrics={"mae": float("nan")}, sample_count=2)
