"""Explicit synthetic CPU acceptance registration. Never registered in production globals."""

import time
from datetime import UTC, datetime, timedelta

from application.model_step_store import canonical
from application.model_steps import ModelStepGrant, implementation_snapshot, spec_digest
from scat_model_runtime import (
    ComponentReference,
    ComponentRegistration,
    ComponentRegistry,
    DatasetKind,
    ModelAvailability,
    ModelExecutionMode,
    ModelInputReference,
    ModelRegistry,
    ModelResourceLimits,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
    RegressionErrorEvaluator,
    RegressionInputSeries,
    RegressionPredictionSeries,
    RegressionTaskInput,
    RegressionTaskOutput,
)
from scat_model_runtime.steps import ModelStepKernel

MODEL_KEY = "synthetic-cpu-cv"
VERSION = "1.0.0"


class SyntheticInput:
    async def resolve(self, *, request):
        return self.build(request=request)

    @staticmethod
    def build(*, request):
        origin = datetime(2026, 1, 1, tzinfo=UTC)
        return RegressionTaskInput(
            feature_names=("x", "y", "alt"),
            feature_units={"x": "m", "y": "m", "alt": "m"},
            series=(
                RegressionInputSeries(
                    input_ref=request.primary_input_ref,
                    timestamps=tuple(origin + timedelta(seconds=i) for i in range(4)),
                    values=((0, 0, 1000), (10, 20, 1005), (20, 40, 1010), (30, 60, 1015)),
                    source_indices=(0, 1, 2, 3),
                    annotations={"synthetic": True, "coordinates": "ENU"},
                ),
            ),
        )


class SyntheticCV:
    calls = 0

    def start(self):
        pass

    def stop(self):
        pass

    def run(self, *, model_input, parameters):
        self.calls += 1
        if parameters:
            raise ValueError("fixture_parameters_not_allowed")
        src = model_input.series[0]
        dt = (src.timestamps[-1] - src.timestamps[-2]).total_seconds()
        velocity = tuple((b - a) / dt for a, b in zip(src.values[-2], src.values[-1], strict=True))
        values = tuple(
            tuple(last + v * seconds for last, v in zip(src.values[-1], velocity, strict=True))
            for seconds in (1, 2)
        )
        return RegressionTaskOutput(
            model_key=MODEL_KEY,
            model_version=VERSION,
            target_names=model_input.feature_names,
            target_units=model_input.feature_units,
            series=(
                RegressionPredictionSeries(
                    input_ref=src.input_ref,
                    forecast_origin=src.timestamps[-1],
                    time_offsets_seconds=(1, 2),
                    values=values,
                    expected_values=((42, 80, 1020), (50, 99, 1025)),
                    annotations={"synthetic": True, "coordinates": "ENU"},
                ),
            ),
        )


def fixture_runtime(scope, run_id, *, expires=None):
    resolver_ref = ComponentReference(key="synthetic-cpu-input", version=VERSION)
    package_ref = ComponentReference(key=MODEL_KEY, version=VERSION)
    evaluator_ref = ComponentReference(key="regression-errors", version="1")
    spec = ModelSpec(
        model_key=MODEL_KEY,
        version=VERSION,
        display_name="Synthetic CPU constant velocity",
        description="Four synthetic ENU observations; isolated S3 acceptance only.",
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        supported_datasets={DatasetKind.SCAT},
        input_schema="regression-task-input/v1",
        output_schema="regression-task-output/v1",
        input_resolver=resolver_ref,
        model_package=package_ref,
        evaluator=evaluator_ref,
        allowed_evaluators=(evaluator_ref,),
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=4, min_input_points=4, max_output_points=2, timeout_seconds=5
        ),
        availability=ModelAvailability.AVAILABLE,
    )
    request = ModelTaskRequest(
        task_type=spec.task_type,
        model_key=MODEL_KEY,
        model_version=VERSION,
        input_ref=ModelInputReference(
            dataset=DatasetKind.SCAT, batch="SYNTH-CPU", external_id="SYNTH-CPU-001", limit=4
        ),
    )
    resolvers, packages, evaluators = (
        ComponentRegistry(label)
        for label in ("synthetic resolver", "synthetic package", "synthetic evaluator")
    )
    package = SyntheticCV()
    resolvers.register(ComponentRegistration(resolver_ref, SyntheticInput()))
    packages.register(ComponentRegistration(package_ref, package))
    evaluators.register(ComponentRegistration(evaluator_ref, RegressionErrorEvaluator()))
    kernel = ModelStepKernel(
        models=ModelRegistry([spec]),
        regression_input_resolvers=resolvers,
        regression_model_packages=packages,
        regression_evaluators=evaluators,
    )
    grant = ModelStepGrant(
        scope,
        run_id,
        canonical(request.model_dump(mode="json")),
        spec_digest(spec),
        expires if expires is not None else time.time() + 600,
        True,
        implementation_snapshot(kernel),
    )
    return kernel, grant, package
