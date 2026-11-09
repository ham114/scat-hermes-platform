"""Explicit production registration and lifecycle for reviewed task-model packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scat_model_runtime.backends.observed import ObservedBackend
from scat_model_runtime.backends.package import BackendRegressionPackage
from scat_model_runtime.backends.registration import UnavailableBackend, configured_http_backend
from scat_model_runtime.backends.registry import execution_backend_registry
from scat_model_runtime.backends.shadow import ShadowBackend
from scat_model_runtime.backends.subprocess import SubprocessBackend
from scat_model_runtime.component_registry import ComponentReference, ComponentRegistration
from scat_model_runtime.contracts import (
    DatasetKind,
    ModelAvailability,
    ModelExecutionMode,
    ModelResourceLimits,
    ModelSpec,
    ModelTaskType,
)
from scat_model_runtime.gbar_v23_adapter import GbarV23SubprocessAdapter, GbarV23WorkerConfig
from scat_model_runtime.model_packages import (
    comparison_input_resolver_registry,
    comparison_model_package_registry,
    regression_input_resolver_registry,
    regression_model_package_registry,
)
from scat_model_runtime.packages.comparison_v1 import (
    COMPARISON_V1_MODEL_KEY,
    COMPARISON_V1_MODEL_VERSION,
    ComparisonV1ModelPackage,
)
from scat_model_runtime.packages.gbar_v23 import (
    GBAR_V23_MODEL_KEY,
    GBAR_V23_MODEL_VERSION,
    GbarV23RegressionPackage,
)
from scat_model_runtime.registry import model_registry
from scat_model_runtime.regression_evaluators import (
    RegressionErrorEvaluator,
    regression_evaluator_registry,
)
from scat_model_runtime.resolvers.scat_comparison import ScatComparisonInputResolver
from scat_model_runtime.resolvers.scat_gbar import ScatGbarRegressionInputResolver
from scat_model_runtime.task_io import (
    COMPARISON_TASK_INPUT_SCHEMA,
    COMPARISON_TASK_OUTPUT_SCHEMA,
    REGRESSION_TASK_INPUT_SCHEMA,
    REGRESSION_TASK_OUTPUT_SCHEMA,
)

SCAT_GBAR_RESOLVER_REFERENCE = ComponentReference(
    key="scat-gbar-raw-series",
    version="1",
)
SCAT_GBAR_PACKAGE_REFERENCE = ComponentReference(
    key=GBAR_V23_MODEL_KEY,
    version=GBAR_V23_MODEL_VERSION,
)
REGRESSION_ERROR_EVALUATOR_REFERENCE = ComponentReference(
    key="regression-errors",
    version="1",
)
SCAT_COMPARISON_RESOLVER_REFERENCE = ComponentReference(
    key="scat-comparison-input",
    version="1",
)
SCAT_COMPARISON_PACKAGE_REFERENCE = ComponentReference(
    key=COMPARISON_V1_MODEL_KEY,
    version=COMPARISON_V1_MODEL_VERSION,
)


@dataclass(slots=True)
class _ProductionRuntime:
    gbar_package: BackendRegressionPackage
    comparison_gbar_package: BackendRegressionPackage | None = None


_runtime: _ProductionRuntime | None = None


def _required_path(settings: Any, name: str) -> Path:
    value = getattr(settings, name, None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required when SCAT_GBAR_V23_ENABLED=true")
    return Path(value)


def _required_text(settings: Any, name: str) -> str:
    value = getattr(settings, name, None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required when SCAT_GBAR_V23_ENABLED=true")
    return value.strip()


def _subprocess_backend(settings):
    adapter = GbarV23SubprocessAdapter(
        GbarV23WorkerConfig(
            python_executable=_required_path(settings, "SCAT_GBAR_V23_PYTHON_EXECUTABLE"),
            worker_script=_required_path(settings, "SCAT_GBAR_V23_WORKER_SCRIPT"),
            source_dir=_required_path(settings, "SCAT_GBAR_V23_SOURCE_DIR"),
            model_config=_required_path(settings, "SCAT_GBAR_V23_CONFIG"),
            checkpoint=_required_path(settings, "SCAT_GBAR_V23_CHECKPOINT"),
            source_uri=_required_text(settings, "SCAT_GBAR_V23_SOURCE_URI"),
            worker_script_sha256=_required_text(settings, "SCAT_GBAR_V23_WORKER_SCRIPT_SHA256"),
            model_config_sha256=_required_text(settings, "SCAT_GBAR_V23_CONFIG_SHA256"),
            checkpoint_sha256=_required_text(settings, "SCAT_GBAR_V23_CHECKPOINT_SHA256"),
            device=str(getattr(settings, "SCAT_GBAR_V23_DEVICE", "cuda")),
            startup_timeout_seconds=float(
                getattr(settings, "SCAT_GBAR_V23_STARTUP_TIMEOUT_SECONDS", 120.0)
            ),
            prediction_timeout_seconds=float(
                getattr(settings, "SCAT_GBAR_V23_PREDICTION_TIMEOUT_SECONDS", 120.0)
            ),
            circuit_failure_threshold=int(
                getattr(settings, "SCAT_GBAR_V23_CIRCUIT_FAILURE_THRESHOLD", 3)
            ),
            circuit_recovery_seconds=float(
                getattr(settings, "SCAT_GBAR_V23_CIRCUIT_RECOVERY_SECONDS", 30.0)
            ),
        )
    )
    return SubprocessBackend(
        GbarV23RegressionPackage(adapter),
        model_key=GBAR_V23_MODEL_KEY,
        model_version=GBAR_V23_MODEL_VERSION,
    )


def configure_production_model_runtime(settings: Any) -> bool:
    """Register reviewed models without loading their heavy checkpoints yet."""
    global _runtime
    if not bool(getattr(settings, "SCAT_GBAR_V23_ENABLED", False)):
        return False
    if _runtime is not None:
        return True

    kind = getattr(settings, "SCAT_GBAR_V23_BACKEND", "subprocess")
    if kind == "subprocess":
        backend = _subprocess_backend(settings)
        reference = ComponentReference(key="gbar-v23-subprocess", version="1")
    else:
        reference = ComponentReference(key="gbar-v23-bento-http", version="1")
        try:
            if kind != "bento_http":
                raise ValueError("unknown backend")
            backend = configured_http_backend(settings)
        except Exception:
            backend = UnavailableBackend()
    if kind == "subprocess" and getattr(settings, "SCAT_GBAR_V23_SHADOW_REFERENCES", ()):
        candidate = None
        try:
            candidate = configured_http_backend(settings)
            backend = ShadowBackend(
                backend,
                candidate,
                references=settings.SCAT_GBAR_V23_SHADOW_REFERENCES,
                report_directory=_required_text(settings, "SCAT_GBAR_V23_SHADOW_REPORT_DIR"),
            )
        except Exception:
            # Optional shadow configuration must not disable the authoritative path.
            if candidate is not None:
                candidate.close()
            import logging

            logging.getLogger(__name__).warning(
                "GBAR shadow disabled: invalid server configuration"
            )
    if getattr(settings, "SCAT_GBAR_V23_OBSERVE", False):
        backend = ObservedBackend(backend)
    health = backend.health()
    availability = ModelAvailability.AVAILABLE if health.ready else ModelAvailability.UNAVAILABLE
    reason = None if health.ready else "模型执行后端不可用：" + health.code
    package = BackendRegressionPackage(backend)
    execution_backend_registry.register(ComponentRegistration(reference, backend, is_default=True))
    resolver = ScatGbarRegressionInputResolver()
    comparison_gbar = None
    comparison_availability = (
        availability if kind == "subprocess" else ModelAvailability.UNAVAILABLE
    )
    comparison_reason = (
        reason if kind == "subprocess" else "远程 GBAR 的对比编排尚未迁移，禁止隐式拆批。"
    )
    if kind == "bento_http" and getattr(
        settings, "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED", False
    ):
        try:
            comparison_backend = _subprocess_backend(settings)
            comparison_health = comparison_backend.health()
            comparison_gbar = BackendRegressionPackage(comparison_backend)
            comparison_availability = (
                ModelAvailability.AVAILABLE
                if comparison_health.ready
                else ModelAvailability.UNAVAILABLE
            )
            comparison_reason = None if comparison_health.ready else "对比子进程不可用。"
        except Exception:
            comparison_reason = "对比子进程配置无效。"
    comparison_package = ComparisonV1ModelPackage(comparison_gbar or package)
    comparison_resolver = ScatComparisonInputResolver()
    regression_input_resolver_registry.register(
        ComponentRegistration(SCAT_GBAR_RESOLVER_REFERENCE, resolver, is_default=True)
    )
    regression_model_package_registry.register(
        ComponentRegistration(SCAT_GBAR_PACKAGE_REFERENCE, package, is_default=True)
    )
    regression_evaluator_registry.register(
        ComponentRegistration(
            REGRESSION_ERROR_EVALUATOR_REFERENCE,
            RegressionErrorEvaluator(),
            is_default=True,
        )
    )
    comparison_input_resolver_registry.register(
        ComponentRegistration(
            SCAT_COMPARISON_RESOLVER_REFERENCE,
            comparison_resolver,
            is_default=True,
        )
    )
    comparison_model_package_registry.register(
        ComponentRegistration(
            SCAT_COMPARISON_PACKAGE_REFERENCE,
            comparison_package,
            is_default=True,
        )
    )
    model_registry.register(
        ModelSpec(
            model_key=GBAR_V23_MODEL_KEY,
            version=GBAR_V23_MODEL_VERSION,
            display_name="SCAT GBAR v23 轨迹预测",
            description="使用GBAR v23预测指定SCAT航班未来36个约10秒轨迹点。",
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            supported_datasets=frozenset({DatasetKind.SCAT}),
            input_schema=REGRESSION_TASK_INPUT_SCHEMA,
            output_schema=REGRESSION_TASK_OUTPUT_SCHEMA,
            input_resolver=SCAT_GBAR_RESOLVER_REFERENCE,
            model_package=SCAT_GBAR_PACKAGE_REFERENCE,
            execution_backend=reference,
            execution_release=getattr(getattr(backend, "config", None), "release_id", None),
            evaluator=REGRESSION_ERROR_EVALUATOR_REFERENCE,
            allowed_evaluators=(REGRESSION_ERROR_EVALUATOR_REFERENCE,),
            execution_mode=ModelExecutionMode.ASYNC,
            resource_limits=ModelResourceLimits(
                min_input_points=71,
                max_input_points=500 if kind == "bento_http" else 2_000,
                max_output_points=36,
                timeout_seconds=backend.config.total_timeout_seconds
                if kind == "bento_http" and hasattr(backend, "config")
                else 120.0,
                max_batch_size=8 if kind == "bento_http" else 16,
            ),
            availability=availability,
            unavailable_reason=reason,
            is_default=True,
        )
    )
    model_registry.register(
        ModelSpec(
            model_key=COMPARISON_V1_MODEL_KEY,
            version=COMPARISON_V1_MODEL_VERSION,
            display_name="SCAT 统一预测对比 v1",
            description="在GBAR统一观测窗和目标时刻下比较GBAR、Kalman、匀速与CFPL。",
            task_type=ModelTaskType.MODEL_COMPARISON,
            supported_datasets=frozenset({DatasetKind.SCAT}),
            input_schema=COMPARISON_TASK_INPUT_SCHEMA,
            output_schema=COMPARISON_TASK_OUTPUT_SCHEMA,
            input_resolver=SCAT_COMPARISON_RESOLVER_REFERENCE,
            model_package=SCAT_COMPARISON_PACKAGE_REFERENCE,
            execution_mode=ModelExecutionMode.ASYNC,
            resource_limits=ModelResourceLimits(
                min_input_points=71,
                max_input_points=2_000,
                max_output_points=144,
                timeout_seconds=3_600.0,
                max_batch_size=50,
            ),
            availability=comparison_availability,
            unavailable_reason=comparison_reason,
            is_default=False,
        )
    )
    _runtime = _ProductionRuntime(gbar_package=package, comparison_gbar_package=comparison_gbar)
    return True


def shutdown_production_model_runtime() -> None:
    if _runtime is not None:
        try:
            _runtime.gbar_package.stop()
        finally:
            if _runtime.comparison_gbar_package is not None:
                _runtime.comparison_gbar_package.stop()


def production_model_runtime_available() -> bool:
    if _runtime is None:
        return False
    return (
        model_registry.get(GBAR_V23_MODEL_KEY, GBAR_V23_MODEL_VERSION).availability
        == ModelAvailability.AVAILABLE
    )


__all__ = [
    "REGRESSION_ERROR_EVALUATOR_REFERENCE",
    "SCAT_COMPARISON_PACKAGE_REFERENCE",
    "SCAT_COMPARISON_RESOLVER_REFERENCE",
    "SCAT_GBAR_PACKAGE_REFERENCE",
    "SCAT_GBAR_RESOLVER_REFERENCE",
    "configure_production_model_runtime",
    "shutdown_production_model_runtime",
]
