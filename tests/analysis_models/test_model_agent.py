"""Tests for the internal model-agent graph without real models or databases."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("langgraph")

from agents.agents import agents
from agents.model_agent import build_model_agent, public_model_agent_result
from scat_model_runtime import (
    COMPARISON_TASK_INPUT_SCHEMA,
    COMPARISON_TASK_OUTPUT_SCHEMA,
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
    Evaluator,
    FeaturePipeline,
    ModelAdapter,
    ModelAvailability,
    ModelEvaluation,
    ModelExecutionMode,
    ModelInferenceOutput,
    ModelInputReference,
    ModelInputResolver,
    ModelParameterValue,
    ModelRegistry,
    ModelResourceLimits,
    ModelRunStatus,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
    PreparedModelInput,
    RegressionErrorEvaluator,
    RegressionEvaluator,
    RegressionInputSeries,
    RegressionModelPackage,
    RegressionPredictionSeries,
    RegressionTaskInput,
    RegressionTaskInputResolver,
    RegressionTaskOutput,
    ResolvedModelInput,
)


class _TestResolver:
    def __init__(self) -> None:
        self.called = False

    async def resolve(self, *, request: ModelTaskRequest) -> ResolvedModelInput:
        self.called = True
        return ResolvedModelInput(
            input_ref=request.input_ref,
            point_count=2,
            payload=(1.0, 2.0),
        )


class _TestPipeline:
    def prepare(
        self,
        *,
        source: ResolvedModelInput,
        request: ModelTaskRequest,
    ) -> PreparedModelInput:
        return PreparedModelInput(
            input_schema="test-features/v1",
            feature_names=("value",),
            point_count=source.point_count,
            payload=source.payload,
        )


class _TestAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.loaded = False
        self.closed = False

    def load(self) -> None:
        self.loaded = True

    def predict(
        self,
        *,
        model_input: PreparedModelInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ModelInferenceOutput:
        if self.fail:
            raise RuntimeError("sensitive /srv/models/private/model.onnx")
        scale = float(parameters.get("scale", 1.0))
        return ModelInferenceOutput(
            output_schema="test-prediction/v1",
            point_count=model_input.point_count,
            payload=tuple(value * scale for value in model_input.payload),
        )

    def close(self) -> None:
        self.closed = True


class _TestEvaluator:
    def evaluate(
        self,
        *,
        prediction: ModelInferenceOutput,
        expected: PreparedModelInput | None = None,
    ) -> ModelEvaluation:
        assert expected is not None
        errors = [
            abs(actual - predicted)
            for actual, predicted in zip(expected.payload, prediction.payload, strict=True)
        ]
        return ModelEvaluation(
            metrics={"mae": sum(errors) / len(errors)},
            sample_count=len(errors),
        )


def _request(*, confirmed: bool = True) -> ModelTaskRequest:
    return ModelTaskRequest(
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        model_key="test-model",
        input_ref=ModelInputReference(
            dataset=DatasetKind.UAV,
            external_id="flight-1",
            limit=2,
            channels=("cmp_pitch",),
        ),
        parameters={"scale": 2.0},
        confirmed=confirmed,
    )


def _build_graph(adapter: ModelAdapter, resolver: ModelInputResolver):
    resolver_ref = ComponentReference(key="test-resolver", version="1.0.0")
    pipeline_ref = ComponentReference(key="test-pipeline", version="1.0.0")
    evaluator_ref = ComponentReference(key="test-evaluator", version="1.0.0")
    model_ref = ComponentReference(key="test-model", version="1.0.0")
    spec = ModelSpec(
        model_key=model_ref.key,
        version=model_ref.version,
        display_name="Test model",
        description="Test-only deterministic model.",
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        supported_datasets={DatasetKind.UAV},
        input_schema="test-features/v1",
        output_schema="test-prediction/v1",
        input_resolver=resolver_ref,
        feature_pipeline=pipeline_ref,
        evaluator=evaluator_ref,
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=10,
            max_output_points=10,
            timeout_seconds=5,
        ),
        availability=ModelAvailability.AVAILABLE,
        is_default=True,
    )
    models = ModelRegistry([spec])
    resolvers = ComponentRegistry[ModelInputResolver]("model input resolver")
    pipelines = ComponentRegistry[FeaturePipeline]("feature pipeline")
    adapters = ComponentRegistry[ModelAdapter]("model adapter")
    evaluators = ComponentRegistry[Evaluator]("evaluator")
    resolvers.register(ComponentRegistration(resolver_ref, resolver))
    pipelines.register(ComponentRegistration(pipeline_ref, _TestPipeline()))
    adapters.register(ComponentRegistration(model_ref, adapter))
    evaluators.register(ComponentRegistration(evaluator_ref, _TestEvaluator()))
    return build_model_agent(
        models=models,
        input_resolvers=resolvers,
        feature_pipelines=pipelines,
        model_adapters=adapters,
        evaluators=evaluators,
    )


@pytest.mark.asyncio
async def test_internal_model_agent_runs_registered_components_in_order():
    resolver = _TestResolver()
    adapter = _TestAdapter()
    graph = _build_graph(adapter, resolver)

    state = await graph.ainvoke({"request": _request()})

    assert state["status"] == ModelRunStatus.SUCCEEDED
    assert state["evaluation"].metrics == {"mae": 1.5}
    assert resolver.called is True
    assert adapter.loaded is True
    assert adapter.closed is True
    public = public_model_agent_result(state)
    assert public["status"] == "succeeded"
    assert "prediction" not in public
    assert "prepared_input" not in public


@pytest.mark.asyncio
async def test_internal_model_agent_preserves_caller_allocated_run_id():
    graph = _build_graph(_TestAdapter(), _TestResolver())

    state = await graph.ainvoke({"request": _request(), "run_id": "a" * 32})

    assert state["run_id"] == "a" * 32


@pytest.mark.asyncio
async def test_internal_model_agent_rejects_unconfirmed_request_before_data_access():
    resolver = _TestResolver()
    graph = _build_graph(_TestAdapter(), resolver)

    state = await graph.ainvoke({"request": _request(confirmed=False)})

    assert state["status"] == ModelRunStatus.FAILED
    assert state["error_code"] == "model_not_runnable"
    assert resolver.called is False


@pytest.mark.asyncio
async def test_internal_model_agent_sanitizes_adapter_failure():
    adapter = _TestAdapter(fail=True)
    graph = _build_graph(adapter, _TestResolver())

    state = await graph.ainvoke({"request": _request()})
    public = public_model_agent_result(state)

    assert public["error_code"] == "model_execution_failed"
    assert public["message"] == "模型执行失败。"
    assert "private" not in str(public)
    assert adapter.closed is True


def test_internal_model_agent_is_not_a_public_agent():
    assert "model-agent" not in agents


class _RegressionResolver:
    async def resolve(self, *, request: ModelTaskRequest) -> RegressionTaskInput:
        return RegressionTaskInput(
            feature_names=("value",),
            feature_units={"value": "unit"},
            series=(
                RegressionInputSeries(
                    input_ref=request.input_ref,
                    timestamps=(
                        request.input_ref.end_time,
                        request.input_ref.end_time,
                    ),
                    values=((1.0,), (2.0,)),
                ),
            ),
        )


class _RegressionPackage:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def run(self, *, model_input, parameters) -> RegressionTaskOutput:
        source = model_input.series[0]
        return RegressionTaskOutput(
            model_key="regression-model",
            model_version="1",
            target_names=("value",),
            target_units={"value": "unit"},
            series=(
                RegressionPredictionSeries(
                    input_ref=source.input_ref,
                    forecast_origin=source.timestamps[-1],
                    time_offsets_seconds=(1.0, 2.0),
                    values=((3.0,), (4.0,)),
                ),
            ),
        )

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_internal_model_agent_runs_regression_package_as_black_box():
    from datetime import UTC, datetime

    from scat_model_runtime import (
        REGRESSION_TASK_INPUT_SCHEMA,
        REGRESSION_TASK_OUTPUT_SCHEMA,
    )

    model_ref = ComponentReference(key="regression-model", version="1")
    resolver_ref = ComponentReference(key="regression-resolver", version="1")
    spec = ModelSpec(
        model_key=model_ref.key,
        version=model_ref.version,
        display_name="Regression model",
        description="Test regression package.",
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        supported_datasets={DatasetKind.SCAT},
        input_schema=REGRESSION_TASK_INPUT_SCHEMA,
        output_schema=REGRESSION_TASK_OUTPUT_SCHEMA,
        input_resolver=resolver_ref,
        model_package=model_ref,
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=10,
            max_output_points=10,
            timeout_seconds=5,
        ),
        availability=ModelAvailability.AVAILABLE,
    )
    resolver = _RegressionResolver()
    package = _RegressionPackage()
    resolvers = ComponentRegistry[RegressionTaskInputResolver]("regression resolver")
    packages = ComponentRegistry[RegressionModelPackage]("regression package")
    resolvers.register(ComponentRegistration(resolver_ref, resolver))
    packages.register(ComponentRegistration(model_ref, package))
    graph = build_model_agent(
        models=ModelRegistry([spec]),
        regression_input_resolvers=resolvers,
        regression_model_packages=packages,
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key=model_ref.key,
        model_version=model_ref.version,
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id="flight",
            end_time=datetime(2026, 9, 15, tzinfo=UTC),
            limit=2,
        ),
        confirmed=True,
    )

    state = await graph.ainvoke({"request": request})

    assert state["status"] == ModelRunStatus.SUCCEEDED
    assert state["regression_output"].series[0].values == ((3.0,), (4.0,))
    assert package.started is True


@pytest.mark.asyncio
async def test_internal_model_agent_runs_allowed_regression_evaluator():
    from datetime import UTC, datetime

    from scat_model_runtime import (
        REGRESSION_TASK_INPUT_SCHEMA,
        REGRESSION_TASK_OUTPUT_SCHEMA,
    )

    class _EvaluablePackage(_RegressionPackage):
        def run(self, *, model_input, parameters) -> RegressionTaskOutput:
            output = super().run(model_input=model_input, parameters=parameters)
            item = output.series[0].model_copy(update={"expected_values": ((2.0,), (5.0,))})
            return output.model_copy(update={"series": (item,)})

    model_ref = ComponentReference(key="regression-model", version="1")
    resolver_ref = ComponentReference(key="regression-resolver", version="1")
    evaluator_ref = ComponentReference(key="regression-errors", version="1")
    spec = ModelSpec(
        model_key=model_ref.key,
        version=model_ref.version,
        display_name="Regression model",
        description="Test regression package.",
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        supported_datasets={DatasetKind.SCAT},
        input_schema=REGRESSION_TASK_INPUT_SCHEMA,
        output_schema=REGRESSION_TASK_OUTPUT_SCHEMA,
        input_resolver=resolver_ref,
        model_package=model_ref,
        evaluator=evaluator_ref,
        allowed_evaluators=(evaluator_ref,),
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=10,
            max_output_points=10,
            timeout_seconds=5,
        ),
        availability=ModelAvailability.AVAILABLE,
    )
    resolvers = ComponentRegistry[RegressionTaskInputResolver]("regression resolver")
    packages = ComponentRegistry[RegressionModelPackage]("regression package")
    evaluators = ComponentRegistry[RegressionEvaluator]("regression evaluator")
    resolvers.register(ComponentRegistration(resolver_ref, _RegressionResolver()))
    packages.register(ComponentRegistration(model_ref, _EvaluablePackage()))
    evaluators.register(ComponentRegistration(evaluator_ref, RegressionErrorEvaluator()))
    graph = build_model_agent(
        models=ModelRegistry([spec]),
        regression_input_resolvers=resolvers,
        regression_model_packages=packages,
        regression_evaluators=evaluators,
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key=model_ref.key,
        model_version=model_ref.version,
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="batch",
            external_id="flight",
            end_time=datetime(2026, 9, 15, tzinfo=UTC),
            limit=2,
        ),
        confirmed=True,
    )

    state = await graph.ainvoke({"request": request})

    assert state["status"] == ModelRunStatus.SUCCEEDED
    error = state["regression_evaluation"].target_errors[0]
    assert error.mae == 1.0
    assert error.rmse == 1.0


@pytest.mark.asyncio
async def test_internal_model_agent_runs_comparison_package_on_its_own_registries():
    origin = datetime(2026, 9, 15, tzinfo=UTC)
    input_ref = ModelInputReference(
        dataset=DatasetKind.SCAT,
        batch="batch",
        external_id="flight-1",
        limit=2,
    )

    def point(seconds: float) -> ComparisonTrajectoryPoint:
        return ComparisonTrajectoryPoint(
            timestamp=origin + timedelta(seconds=seconds),
            latitude_deg=60.0 + seconds / 10_000,
            longitude_deg=20.0,
            altitude_m=3_000.0,
        )

    comparison_input = ComparisonTaskInput(
        feature_names=("lat", "lon"),
        feature_units={"lat": "degree", "lon": "degree"},
        samples=(
            ComparisonSampleInput(
                sample_id="flight-1:cutoff-1",
                input_ref=input_ref,
                forecast_origin=origin,
                observed_timestamps=(origin - timedelta(seconds=5), origin),
                observed_values=((59.9, 19.9), (60.0, 20.0)),
                actual_future_candidates=(point(10),),
                cfpl_points=(point(0), point(20)),
            ),
        ),
    )
    distribution = ComparisonMetricDistribution(mean=10, median=10, p90=10)
    method_ids = ("gbar", "kalman-cv", "constant-velocity-heading", "cfpl")
    comparison_output = ComparisonTaskOutput(
        model_key="comparison-model",
        model_version="1",
        sample_set_sha256="0" * 64,
        samples=(
            ComparisonSampleResult(
                sample_id="flight-1:cutoff-1",
                input_ref=input_ref,
                forecast_origin=origin,
                time_offsets_seconds=(10.0,),
                actual_points=(point(10),),
                method_results=tuple(
                    ComparisonMethodResult(
                        method_id=method_id,
                        method_version="1",
                        predicted_points=(point(10),),
                        ade_metres=10,
                        fde_metres=10,
                    )
                    for method_id in method_ids
                ),
            ),
        ),
        method_summaries=tuple(
            ComparisonMethodSummary(
                method_id=method_id,
                display_name=method_id,
                method_version="1",
                sample_count=1,
                covered_flights=1,
                ade_metres=distribution,
                fde_metres=distribution,
            )
            for method_id in method_ids
        ),
    )

    class Resolver:
        async def resolve(self, *, request: ModelTaskRequest) -> ComparisonTaskInput:
            return comparison_input

    class Package:
        started = False

        def start(self) -> None:
            self.started = True

        def run(self, *, model_input, parameters) -> ComparisonTaskOutput:
            assert model_input is comparison_input
            assert parameters == {}
            return comparison_output

        def stop(self) -> None:
            pass

    model_ref = ComponentReference(key="comparison-model", version="1")
    resolver_ref = ComponentReference(key="comparison-resolver", version="1")
    spec = ModelSpec(
        model_key=model_ref.key,
        version=model_ref.version,
        display_name="Comparison model",
        description="Test comparison package.",
        task_type=ModelTaskType.MODEL_COMPARISON,
        supported_datasets={DatasetKind.SCAT},
        input_schema=COMPARISON_TASK_INPUT_SCHEMA,
        output_schema=COMPARISON_TASK_OUTPUT_SCHEMA,
        input_resolver=resolver_ref,
        model_package=model_ref,
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=10,
            max_output_points=10,
            timeout_seconds=5,
        ),
        availability=ModelAvailability.AVAILABLE,
    )
    resolvers = ComponentRegistry[ComparisonTaskInputResolver]("comparison resolver")
    packages = ComponentRegistry[ComparisonModelPackage]("comparison package")
    resolver = Resolver()
    package = Package()
    resolvers.register(ComponentRegistration(resolver_ref, resolver))
    packages.register(ComponentRegistration(model_ref, package))
    graph = build_model_agent(
        models=ModelRegistry([spec]),
        comparison_input_resolvers=resolvers,
        comparison_model_packages=packages,
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.MODEL_COMPARISON,
        model_key=model_ref.key,
        model_version=model_ref.version,
        input_ref=input_ref,
        confirmed=True,
    )

    state = await graph.ainvoke({"request": request})

    assert state["status"] == ModelRunStatus.SUCCEEDED
    assert state["comparison_output"] is comparison_output
    assert package.started is True
