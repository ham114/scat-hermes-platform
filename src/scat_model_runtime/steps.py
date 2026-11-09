"""Neutral numerical kernel extracted from the frozen H5 graph for S3.

The old graph remains an independent numerical oracle; this module imports no agents.
"""

from __future__ import annotations

import logging
from asyncio import to_thread, wait_for
from typing import TypedDict

from scat_model_runtime import (
    ComparisonModelPackage,
    ComparisonTaskInput,
    ComparisonTaskInputResolver,
    ComparisonTaskOutput,
    ComponentReference,
    ComponentRegistry,
    ComponentRegistryError,
    Evaluator,
    FeaturePipeline,
    ModelAdapter,
    ModelEvaluation,
    ModelInferenceOutput,
    ModelInputResolver,
    ModelRegistry,
    ModelRegistryError,
    ModelRunStatus,
    ModelSpec,
    ModelTaskDataError,
    ModelTaskRequest,
    ModelTaskType,
    PreparedModelInput,
    RegressionEvaluation,
    RegressionEvaluator,
    RegressionModelPackage,
    RegressionTaskInput,
    RegressionTaskInputResolver,
    RegressionTaskOutput,
    ResolvedModelInput,
    comparison_input_resolver_registry,
    comparison_model_package_registry,
    evaluator_registry,
    feature_pipeline_registry,
    input_resolver_registry,
    model_adapter_registry,
    model_registry,
    regression_evaluator_registry,
    regression_input_resolver_registry,
    regression_model_package_registry,
)
from scat_model_runtime.backends.package import execute_with_identity

logger = logging.getLogger(__name__)


class StepState(TypedDict, total=False):
    """Internal state; runtime payload fields must never be returned to the LLM."""

    request: ModelTaskRequest
    run_id: str
    status: ModelRunStatus
    progress: float
    model_spec: ModelSpec
    source: ResolvedModelInput
    prepared_input: PreparedModelInput
    prediction: ModelInferenceOutput
    evaluation: ModelEvaluation
    regression_input: RegressionTaskInput
    regression_output: RegressionTaskOutput
    regression_evaluation: RegressionEvaluation
    comparison_input: ComparisonTaskInput
    comparison_output: ComparisonTaskOutput
    error_code: str
    message: str


def _failed(*, code: str, message: str) -> StepState:
    return {
        "status": ModelRunStatus.FAILED,
        "progress": 1.0,
        "error_code": code,
        "message": message,
    }


def _regression_evaluator_reference(
    request: ModelTaskRequest, spec: ModelSpec
) -> ComponentReference | None:
    """Use a requested evaluator or the model package's default evaluator."""
    if request.evaluator is not None:
        return request.evaluator
    return spec.evaluator


class ModelStepKernel:
    """Independent numerical operations. No graph or workflow scheduler."""

    def __init__(
        self,
        *,
        models: ModelRegistry = model_registry,
        input_resolvers: ComponentRegistry[ModelInputResolver] = input_resolver_registry,
        feature_pipelines: ComponentRegistry[FeaturePipeline] = feature_pipeline_registry,
        model_adapters: ComponentRegistry[ModelAdapter] = model_adapter_registry,
        evaluators: ComponentRegistry[Evaluator] = evaluator_registry,
        regression_input_resolvers: ComponentRegistry[
            RegressionTaskInputResolver
        ] = regression_input_resolver_registry,
        regression_model_packages: ComponentRegistry[
            RegressionModelPackage
        ] = regression_model_package_registry,
        regression_evaluators: ComponentRegistry[
            RegressionEvaluator
        ] = regression_evaluator_registry,
        comparison_input_resolvers: ComponentRegistry[
            ComparisonTaskInputResolver
        ] = comparison_input_resolver_registry,
        comparison_model_packages: ComponentRegistry[
            ComparisonModelPackage
        ] = comparison_model_package_registry,
    ):
        self.comparison_input_resolvers = comparison_input_resolvers
        self.comparison_model_packages = comparison_model_packages
        self.evaluators = evaluators
        self.feature_pipelines = feature_pipelines
        self.input_resolvers = input_resolvers
        self.model_adapters = model_adapters
        self.models = models
        self.regression_evaluators = regression_evaluators
        self.regression_input_resolvers = regression_input_resolvers
        self.regression_model_packages = regression_model_packages

    def validate_request(self, state: StepState) -> StepState:
        request = state.get("request")
        if request is None:
            return _failed(code="invalid_model_task", message="模型任务请求缺失。")
        try:
            spec = self.models.resolve(request)
        except ModelRegistryError as exc:
            return _failed(code="model_not_runnable", message=str(exc))
        return {"model_spec": spec, "status": ModelRunStatus.PREPARING_DATA, "progress": 0.1}

    def resolve_components(self, state: StepState) -> StepState:
        request = state["request"]
        spec = state["model_spec"]
        if spec.task_type == ModelTaskType.MODEL_COMPARISON:
            if spec.input_resolver is None or spec.model_package is None:
                return _failed(
                    code="model_configuration_incomplete",
                    message="对比模型尚未配置任务输入解析器或执行包。",
                )
            if request.evaluator is not None:
                return _failed(
                    code="evaluator_not_allowed", message="统一预测对比不接受独立评估器。"
                )
            try:
                self.comparison_input_resolvers.resolve(spec.input_resolver)
                self.comparison_model_packages.resolve(spec.model_package)
            except ComponentRegistryError as exc:
                return _failed(code="component_unavailable", message=str(exc))
            return {}
        if spec.model_package is not None:
            if spec.input_resolver is None:
                return _failed(
                    code="model_configuration_incomplete", message="模型尚未配置任务输入解析器。"
                )
            evaluator_ref = _regression_evaluator_reference(request, spec)
            if evaluator_ref is not None and evaluator_ref not in spec.allowed_evaluators:
                return _failed(
                    code="evaluator_not_allowed", message="请求的评估器不在该模型允许范围内。"
                )
            try:
                self.regression_input_resolvers.resolve(spec.input_resolver)
                self.regression_model_packages.resolve(spec.model_package)
                if evaluator_ref is not None:
                    self.regression_evaluators.resolve(evaluator_ref)
            except ComponentRegistryError as exc:
                return _failed(code="component_unavailable", message=str(exc))
            return {}
        if spec.input_resolver is None or spec.feature_pipeline is None:
            return _failed(
                code="model_configuration_incomplete",
                message="模型尚未配置数据解析器或特征流水线。",
            )
        evaluator_ref = request.evaluator or spec.evaluator
        if request.evaluator is not None and request.evaluator != spec.evaluator:
            return _failed(
                code="evaluator_not_allowed", message="请求的评估器不在该模型允许范围内。"
            )
        try:
            self.input_resolvers.resolve(spec.input_resolver)
            self.feature_pipelines.resolve(spec.feature_pipeline)
            self.model_adapters.resolve(
                ComponentReference(key=spec.model_key, version=spec.version)
            )
            if evaluator_ref is not None:
                self.evaluators.resolve(evaluator_ref)
        except ComponentRegistryError as exc:
            return _failed(code="component_unavailable", message=str(exc))
        return {}

    async def resolve_input(self, state: StepState) -> StepState:
        try:
            spec = state["model_spec"]
            if spec.task_type == ModelTaskType.MODEL_COMPARISON:
                if spec.input_resolver is None:
                    raise ValueError("comparison task input resolver is missing")
                comparison_resolver = self.comparison_input_resolvers.resolve(spec.input_resolver)
                comparison_input = await comparison_resolver.resolve(request=state["request"])
                if comparison_input.schema_version != spec.input_schema:
                    raise ValueError("comparison task input schema mismatch")
                if any(
                    len(sample.observed_values) > spec.resource_limits.max_input_points
                    for sample in comparison_input.samples
                ):
                    raise ValueError("resolved input exceeds max_input_points")
                if comparison_input.batch_size > len(state["request"].references) * 3:
                    raise ValueError("resolved comparison input exceeds cutoff limit")
                requested = {
                    (item.dataset, item.batch, item.external_id)
                    for item in state["request"].references
                }
                if any(
                    (sample.input_ref.dataset, sample.input_ref.batch, sample.input_ref.external_id)
                    not in requested
                    for sample in comparison_input.samples
                ):
                    raise ValueError("resolved input reference changed")
                return {
                    "comparison_input": comparison_input,
                    "status": ModelRunStatus.PREPARING_DATA,
                    "progress": 0.25,
                }
            if spec.model_package is not None:
                if spec.input_resolver is None:
                    raise ValueError("regression task input resolver is missing")
                regression_resolver = self.regression_input_resolvers.resolve(spec.input_resolver)
                evaluator_ref = _regression_evaluator_reference(state["request"], spec)
                resolver_request = state["request"].model_copy(update={"evaluator": evaluator_ref})
                regression_input = await regression_resolver.resolve(request=resolver_request)
                if regression_input.schema_version != spec.input_schema:
                    raise ValueError("regression task input schema mismatch")
                if any(
                    len(item.values) > spec.resource_limits.max_input_points
                    for item in regression_input.series
                ):
                    raise ValueError("resolved input exceeds max_input_points")
                if regression_input.batch_size > spec.resource_limits.max_batch_size:
                    raise ValueError("resolved input exceeds max_batch_size")
                if (
                    tuple(item.input_ref for item in regression_input.series)
                    != state["request"].references
                ):
                    raise ValueError("resolved input reference changed")
                return {
                    "regression_input": regression_input,
                    "status": ModelRunStatus.PREPARING_DATA,
                    "progress": 0.25,
                }
            if spec.input_resolver is None:
                raise ValueError("model input resolver is missing")
            if len(state["request"].references) != 1:
                raise ValueError("legacy model runtime does not support batch input")
            input_resolver = self.input_resolvers.resolve(spec.input_resolver)
            source = await input_resolver.resolve(request=state["request"])
            if source.input_ref != state["request"].primary_input_ref:
                raise ValueError("resolved input reference changed")
            if source.point_count > spec.resource_limits.max_input_points:
                raise ValueError("resolved input exceeds max_input_points")
            if source.batch_size > spec.resource_limits.max_batch_size:
                raise ValueError("resolved input exceeds max_batch_size")
        except Exception as exc:
            logger.exception("Model input resolution failed")
            detail = f"：{exc}" if isinstance(exc, ModelTaskDataError) else "。"
            return _failed(code="input_resolution_failed", message=f"模型输入数据准备失败{detail}")
        return {"source": source, "status": ModelRunStatus.PREPARING_DATA, "progress": 0.25}

    def prepare_features(self, state: StepState) -> StepState:
        spec = state["model_spec"]
        if spec.task_type == ModelTaskType.MODEL_COMPARISON:
            return {"status": ModelRunStatus.LOADING_MODEL, "progress": 0.45}
        if spec.model_package is not None:
            return {"status": ModelRunStatus.LOADING_MODEL, "progress": 0.45}
        try:
            if spec.feature_pipeline is None:
                raise ValueError("feature pipeline is missing")
            pipeline = self.feature_pipelines.resolve(spec.feature_pipeline)
            prepared = pipeline.prepare(source=state["source"], request=state["request"])
            if prepared.input_schema != spec.input_schema:
                raise ValueError("prepared input schema mismatch")
            if prepared.point_count > spec.resource_limits.max_input_points:
                raise ValueError("prepared input exceeds max_input_points")
            if prepared.batch_size > spec.resource_limits.max_batch_size:
                raise ValueError("prepared input exceeds max_batch_size")
        except Exception:
            logger.exception("Model feature preparation failed")
            return _failed(code="feature_preparation_failed", message="模型特征处理失败。")
        return {
            "prepared_input": prepared,
            "status": ModelRunStatus.LOADING_MODEL,
            "progress": 0.45,
        }

    async def execute_model(self, state: StepState) -> StepState:
        spec = state["model_spec"]
        if spec.task_type == ModelTaskType.MODEL_COMPARISON:
            try:
                if spec.model_package is None:
                    raise ValueError("comparison model package is missing")
                comparison_package = self.comparison_model_packages.resolve(spec.model_package)
                await to_thread(comparison_package.start)
                comparison_output = await wait_for(
                    to_thread(
                        comparison_package.run,
                        model_input=state["comparison_input"],
                        parameters=state["request"].parameters,
                    ),
                    timeout=spec.resource_limits.timeout_seconds,
                )
                if comparison_output.schema_version != spec.output_schema:
                    raise ValueError("comparison task output schema mismatch")
                if comparison_output.batch_size > state["comparison_input"].batch_size:
                    raise ValueError("comparison output contains unknown samples")
                input_sample_ids = {
                    sample.sample_id for sample in state["comparison_input"].samples
                }
                if any(
                    sample.sample_id not in input_sample_ids for sample in comparison_output.samples
                ):
                    raise ValueError("comparison output contains unknown samples")
                if any(
                    sum(len(method.predicted_points) for method in sample.method_results)
                    > spec.resource_limits.max_output_points
                    for sample in comparison_output.samples
                ):
                    raise ValueError("model output exceeds max_output_points")
            except Exception as exc:
                logger.exception("Registered comparison model package execution failed")
                detail = f"：{exc}" if isinstance(exc, ModelTaskDataError) else "。"
                return _failed(
                    code="model_execution_failed", message=f"统一预测对比执行失败{detail}"
                )
            return {
                "comparison_output": comparison_output,
                "status": ModelRunStatus.EVALUATING,
                "progress": 0.75,
            }
        if spec.model_package is not None:
            try:
                regression_package = self.regression_model_packages.resolve(spec.model_package)
                await to_thread(regression_package.start)
                regression_output = await execute_with_identity(
                    regression_package,
                    model_input=state["regression_input"],
                    parameters=state["request"].parameters,
                    run_id=state["run_id"],
                    timeout=spec.resource_limits.timeout_seconds,
                )
                if regression_output.schema_version != spec.output_schema:
                    raise ValueError("regression task output schema mismatch")
                if any(
                    len(item.values) > spec.resource_limits.max_output_points
                    for item in regression_output.series
                ):
                    raise ValueError("model output exceeds max_output_points")
                if regression_output.batch_size > spec.resource_limits.max_batch_size:
                    raise ValueError("model output exceeds max_batch_size")
            except Exception as exc:
                logger.exception("Registered regression model package execution failed")
                detail = f"：{exc}" if isinstance(exc, ModelTaskDataError) else "。"
                return _failed(code="model_execution_failed", message=f"模型执行失败{detail}")
            return {
                "regression_output": regression_output,
                "status": ModelRunStatus.EVALUATING,
                "progress": 0.75,
            }
        adapter = self.model_adapters.resolve(
            ComponentReference(key=spec.model_key, version=spec.version)
        )
        try:
            adapter.load()
            prediction = adapter.predict(
                model_input=state["prepared_input"], parameters=state["request"].parameters
            )
            if prediction.output_schema != spec.output_schema:
                raise ValueError("model output schema mismatch")
            if prediction.point_count > spec.resource_limits.max_output_points:
                raise ValueError("model output exceeds max_output_points")
            if prediction.batch_size > spec.resource_limits.max_batch_size:
                raise ValueError("model output exceeds max_batch_size")
        except Exception:
            logger.exception("Registered model adapter execution failed")
            return _failed(code="model_execution_failed", message="模型执行失败。")
        finally:
            try:
                adapter.close()
            except Exception:
                logger.exception("Registered model adapter cleanup failed")
        return {"prediction": prediction, "status": ModelRunStatus.EVALUATING, "progress": 0.75}

    def evaluate_model(self, state: StepState) -> StepState:
        if "comparison_output" in state:
            return {"status": ModelRunStatus.PERSISTING, "progress": 0.9}
        if "regression_output" in state:
            evaluator_ref = _regression_evaluator_reference(state["request"], state["model_spec"])
            if evaluator_ref is not None:
                try:
                    regression_evaluator = self.regression_evaluators.resolve(evaluator_ref)
                    regression_evaluation = regression_evaluator.evaluate(
                        output=state["regression_output"]
                    )
                except Exception as exc:
                    logger.exception("Registered regression evaluator failed")
                    detail = f"：{exc}" if isinstance(exc, ModelTaskDataError) else "。"
                    return _failed(
                        code="model_evaluation_failed", message=f"模型误差分析失败{detail}"
                    )
                return {
                    "regression_evaluation": regression_evaluation,
                    "status": ModelRunStatus.PERSISTING,
                    "progress": 0.9,
                }
            return {"status": ModelRunStatus.PERSISTING, "progress": 0.9}
        request = state["request"]
        spec = state["model_spec"]
        evaluator_ref = request.evaluator or spec.evaluator
        if evaluator_ref is None:
            return {"status": ModelRunStatus.PERSISTING, "progress": 0.9}
        try:
            legacy_evaluator = self.evaluators.resolve(evaluator_ref)
            evaluation = legacy_evaluator.evaluate(
                prediction=state["prediction"], expected=state["prepared_input"]
            )
        except Exception:
            logger.exception("Registered model evaluator failed")
            return _failed(code="model_evaluation_failed", message="模型评估失败。")
        return {"evaluation": evaluation, "status": ModelRunStatus.PERSISTING, "progress": 0.9}
