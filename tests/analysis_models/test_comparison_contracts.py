from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from scat_model_runtime import (
    COMPARISON_TASK_INPUT_SCHEMA,
    COMPARISON_TASK_OUTPUT_SCHEMA,
    GBAR_ALIGNMENT_PROTOCOL_VERSION,
    ComparisonMethodResult,
    ComparisonMethodSummary,
    ComparisonMetricDistribution,
    ComparisonModelPackage,
    ComparisonSampleInput,
    ComparisonSampleResult,
    ComparisonTaskInput,
    ComparisonTaskInputResolver,
    ComparisonTaskOutput,
    ComparisonTrajectoryPoint,
    ComponentReference,
    ComponentRegistration,
    ComponentRegistry,
    DatasetKind,
    ModelInputReference,
    ModelParameterValue,
    ModelTaskRequest,
    comparison_input_resolver_registry,
    comparison_model_package_registry,
    regression_input_resolver_registry,
    regression_model_package_registry,
)


def _input_ref(external_id: str = "100002") -> ModelInputReference:
    return ModelInputReference(
        dataset=DatasetKind.SCAT,
        batch="scat20161015_20161021",
        external_id=external_id,
    )


def _point(timestamp: datetime, latitude: float = 60.0) -> ComparisonTrajectoryPoint:
    return ComparisonTrajectoryPoint(
        timestamp=timestamp,
        latitude_deg=latitude,
        longitude_deg=20.0,
        altitude_m=3_000.0,
    )


def _task_input() -> ComparisonTaskInput:
    origin = datetime(2016, 10, 20, 20, 0, tzinfo=UTC)
    return ComparisonTaskInput(
        feature_names=("lat", "lon"),
        feature_units={"lat": "degree", "lon": "degree"},
        samples=(
            ComparisonSampleInput(
                sample_id="100002:cutoff-1",
                input_ref=_input_ref(),
                forecast_origin=origin,
                observed_timestamps=(
                    origin - timedelta(seconds=5),
                    origin,
                ),
                observed_values=((59.9, 19.9), (60.0, 20.0)),
                source_indices=(69, 70),
                actual_future_candidates=(
                    _point(origin + timedelta(seconds=10), 60.01),
                    _point(origin + timedelta(seconds=20), 60.02),
                ),
                cfpl_points=(
                    _point(origin - timedelta(seconds=60), 59.9),
                    _point(origin + timedelta(seconds=600), 60.5),
                ),
            ),
        ),
    )


def _method_result(
    method_id: str,
    origin: datetime,
    *,
    offset_seconds: int = 10,
) -> ComparisonMethodResult:
    return ComparisonMethodResult(
        method_id=method_id,
        method_version="1",
        predicted_points=(_point(origin + timedelta(seconds=offset_seconds), 60.01),),
        ade_metres=100.0,
        fde_metres=100.0,
    )


def _summary(method_id: str) -> ComparisonMethodSummary:
    distribution = ComparisonMetricDistribution(mean=100.0, median=90.0, p90=120.0)
    return ComparisonMethodSummary(
        method_id=method_id,
        display_name=method_id,
        method_version="1",
        sample_count=1,
        covered_flights=1,
        ade_metres=distribution,
        fde_metres=distribution,
        version_info={"protocol": GBAR_ALIGNMENT_PROTOCOL_VERSION},
    )


def _task_output() -> ComparisonTaskOutput:
    origin = _task_input().samples[0].forecast_origin
    methods = ("gbar", "kalman-cv", "constant-velocity-heading", "cfpl")
    return ComparisonTaskOutput(
        model_key="scat-comparison-v1",
        model_version="1",
        sample_set_sha256="0" * 64,
        samples=(
            ComparisonSampleResult(
                sample_id="100002:cutoff-1",
                input_ref=_input_ref(),
                forecast_origin=origin,
                time_offsets_seconds=(10.0,),
                actual_points=(_point(origin + timedelta(seconds=10), 60.01),),
                method_results=tuple(_method_result(method, origin) for method in methods),
            ),
        ),
        method_summaries=tuple(_summary(method) for method in methods),
        excluded_counts={"truth_missing": 0},
    )


def test_comparison_contracts_expose_frozen_schema_and_protocol_versions():
    model_input = _task_input()
    output = _task_output()

    assert model_input.schema_version == COMPARISON_TASK_INPUT_SCHEMA
    assert output.schema_version == COMPARISON_TASK_OUTPUT_SCHEMA
    assert model_input.protocol_version == GBAR_ALIGNMENT_PROTOCOL_VERSION
    assert output.protocol_version == GBAR_ALIGNMENT_PROTOCOL_VERSION
    assert model_input.batch_size == 1
    assert output.batch_size == 1


def test_comparison_input_rejects_a_different_observation_width():
    payload = _task_input().model_dump(mode="python")
    payload["samples"][0]["observed_values"] = ((59.9,), (60.0,))

    with pytest.raises(ValidationError, match="feature_names width"):
        ComparisonTaskInput.model_validate(payload)


def test_comparison_result_requires_every_method_to_use_one_shared_axis():
    origin = _task_input().samples[0].forecast_origin

    with pytest.raises(ValidationError, match="shared output axis"):
        ComparisonSampleResult(
            sample_id="100002:cutoff-1",
            input_ref=_input_ref(),
            forecast_origin=origin,
            time_offsets_seconds=(10.0,),
            actual_points=(_point(origin + timedelta(seconds=10)),),
            method_results=(_method_result("gbar", origin, offset_seconds=20),),
        )


def test_comparison_method_point_errors_must_align_with_prediction_axis():
    origin = _task_input().samples[0].forecast_origin
    payload = _method_result("gbar", origin).model_dump(mode="python")
    payload["horizontal_errors_metres"] = (10.0, 20.0)

    with pytest.raises(ValidationError, match="point errors must align"):
        ComparisonMethodResult.model_validate(payload)


def test_comparison_method_point_errors_must_match_ade_and_fde():
    origin = _task_input().samples[0].forecast_origin
    payload = _method_result("gbar", origin).model_dump(mode="python")
    payload["horizontal_errors_metres"] = (50.0,)

    with pytest.raises(ValidationError, match="ADE must equal"):
        ComparisonMethodResult.model_validate(payload)


def test_comparison_output_rejects_method_set_drift_between_samples_and_summary():
    payload = _task_output().model_dump(mode="python")
    payload["method_summaries"] = payload["method_summaries"][:-1]

    with pytest.raises(ValidationError, match="summarized methods"):
        ComparisonTaskOutput.model_validate(payload)


class _FakeComparisonPackage:
    def start(self) -> None:
        pass

    def run(
        self,
        *,
        model_input: ComparisonTaskInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ComparisonTaskOutput:
        assert model_input.batch_size == 1
        assert parameters == {}
        return _task_output()

    def stop(self) -> None:
        pass


class _FakeComparisonResolver:
    async def resolve(self, *, request: ModelTaskRequest) -> ComparisonTaskInput:
        assert request.model_key == "scat-comparison-v1"
        return _task_input()


def test_comparison_packages_and_resolvers_have_task_specific_registries():
    package_registry = ComponentRegistry[ComparisonModelPackage]("comparison package")
    resolver_registry = ComponentRegistry[ComparisonTaskInputResolver]("comparison resolver")
    package_ref = ComponentReference(key="test-comparison-package", version="1")
    resolver_ref = ComponentReference(key="test-comparison-resolver", version="1")
    package = _FakeComparisonPackage()
    resolver = _FakeComparisonResolver()

    package_registry.register(ComponentRegistration(package_ref, package))
    resolver_registry.register(ComponentRegistration(resolver_ref, resolver))

    assert package_registry.resolve(package_ref) is package
    assert resolver_registry.resolve(resolver_ref) is resolver
    assert comparison_model_package_registry is not regression_model_package_registry
    assert comparison_input_resolver_registry is not regression_input_resolver_registry
