from types import SimpleNamespace

from scat_model_runtime import production
from scat_model_runtime.component_registry import ComponentRegistry
from scat_model_runtime.contracts import ModelTaskType
from scat_model_runtime.registry import ModelRegistry


class _FakeAdapter:
    def __init__(self) -> None:
        self.shutdown_count = 0

    def shutdown(self) -> None:
        self.shutdown_count += 1


def test_production_registration_adds_comparison_model_and_components(monkeypatch):
    adapter = _FakeAdapter()
    models = ModelRegistry()
    regression_resolvers = ComponentRegistry[object]("regression resolver")
    regression_packages = ComponentRegistry[object]("regression package")
    regression_evaluators = ComponentRegistry[object]("regression evaluator")
    comparison_resolvers = ComponentRegistry[object]("comparison resolver")
    comparison_packages = ComponentRegistry[object]("comparison package")
    monkeypatch.setattr(production, "_runtime", None)
    monkeypatch.setattr(production, "model_registry", models)
    monkeypatch.setattr(production, "regression_input_resolver_registry", regression_resolvers)
    monkeypatch.setattr(production, "regression_model_package_registry", regression_packages)
    monkeypatch.setattr(production, "regression_evaluator_registry", regression_evaluators)
    monkeypatch.setattr(production, "comparison_input_resolver_registry", comparison_resolvers)
    monkeypatch.setattr(production, "comparison_model_package_registry", comparison_packages)
    monkeypatch.setattr(production, "GbarV23SubprocessAdapter", lambda _config: adapter)
    monkeypatch.setattr(production, "ScatGbarRegressionInputResolver", object)
    monkeypatch.setattr(production, "ScatComparisonInputResolver", object)
    settings = SimpleNamespace(
        SCAT_GBAR_V23_ENABLED=True,
        SCAT_GBAR_V23_PYTHON_EXECUTABLE="python",
        SCAT_GBAR_V23_WORKER_SCRIPT="worker.py",
        SCAT_GBAR_V23_SOURCE_DIR="source",
        SCAT_GBAR_V23_CONFIG="config.json",
        SCAT_GBAR_V23_CHECKPOINT="checkpoint.pt",
        SCAT_GBAR_V23_SOURCE_URI="project-internal://scat-gbar-v23",
        SCAT_GBAR_V23_WORKER_SCRIPT_SHA256="a" * 64,
        SCAT_GBAR_V23_CONFIG_SHA256="b" * 64,
        SCAT_GBAR_V23_CHECKPOINT_SHA256="c" * 64,
    )

    assert production.configure_production_model_runtime(settings) is True

    comparison = models.get("scat-comparison-v1", "1")
    assert comparison.task_type == ModelTaskType.MODEL_COMPARISON
    assert comparison.input_schema == "comparison-task-input/v1"
    assert comparison.output_schema == "comparison-task-output/v1"
    assert comparison.execution_mode == "async"
    assert comparison.availability == "available"
    assert comparison.unavailable_reason is None
    assert comparison.resource_limits.max_batch_size == 50
    assert comparison_resolvers.resolve(production.SCAT_COMPARISON_RESOLVER_REFERENCE) is not None
    assert comparison_packages.resolve(production.SCAT_COMPARISON_PACKAGE_REFERENCE) is not None

    production.shutdown_production_model_runtime()
    assert adapter.shutdown_count == 1
