"""Explicit allowlist for task models managed by the internal model-agent."""

from __future__ import annotations

from collections.abc import Iterable

from scat_model_runtime.contracts import (
    DatasetKind,
    ModelAvailability,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
)


class ModelRegistryError(ValueError):
    """Base error for safe model registry failures."""


class DuplicateModelError(ModelRegistryError):
    pass


class ModelNotFoundError(ModelRegistryError):
    pass


class ModelVersionRequiredError(ModelRegistryError):
    pass


class ModelNotRunnableError(ModelRegistryError):
    pass


class ModelRegistry:
    """In-memory model metadata registry; adapters are added in the next slice."""

    def __init__(self, specs: Iterable[ModelSpec] = ()) -> None:
        self._specs: dict[tuple[str, str], ModelSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ModelSpec) -> None:
        key = (spec.model_key, spec.version)
        if key in self._specs:
            raise DuplicateModelError(f"model already registered: {spec.registry_id}")
        if spec.is_default and any(
            current.model_key == spec.model_key and current.is_default
            for current in self._specs.values()
        ):
            raise DuplicateModelError(f"default model version already registered: {spec.model_key}")
        self._specs[key] = spec

    def get(self, model_key: str, version: str | None = None) -> ModelSpec:
        if version is not None:
            spec = self._specs.get((model_key, version))
            if spec is None:
                raise ModelNotFoundError(f"model not registered: {model_key}:{version}")
            return spec

        candidates = [spec for spec in self._specs.values() if spec.model_key == model_key]
        if not candidates:
            raise ModelNotFoundError(f"model not registered: {model_key}")
        defaults = [spec for spec in candidates if spec.is_default]
        if len(defaults) == 1:
            return defaults[0]
        if len(candidates) == 1:
            return candidates[0]
        raise ModelVersionRequiredError(f"model version is required for model_key: {model_key}")

    def list(
        self,
        *,
        task_type: ModelTaskType | None = None,
        dataset: DatasetKind | None = None,
        availability: ModelAvailability | None = None,
    ) -> tuple[ModelSpec, ...]:
        specs = list(self._specs.values())
        if task_type is not None:
            specs = [spec for spec in specs if spec.task_type == task_type]
        if dataset is not None:
            specs = [spec for spec in specs if dataset in spec.supported_datasets]
        if availability is not None:
            specs = [spec for spec in specs if spec.availability == availability]
        return tuple(sorted(specs, key=lambda spec: (spec.model_key, spec.version)))

    def resolve(self, request: ModelTaskRequest) -> ModelSpec:
        """Resolve a task to a registered, compatible and available model."""
        if not request.confirmed:
            raise ModelNotRunnableError("model task has not been confirmed")
        return self.match(request)

    def match(self, request: ModelTaskRequest) -> ModelSpec:
        """Validate model compatibility without authorizing execution."""
        spec = self.get(request.model_key, request.model_version)
        if spec.task_type != request.task_type:
            raise ModelNotRunnableError(
                f"model task mismatch: expected {spec.task_type}, got {request.task_type}"
            )
        references = request.references
        unsupported = [
            reference.dataset
            for reference in references
            if reference.dataset not in spec.supported_datasets
        ]
        if unsupported:
            raise ModelNotRunnableError(f"model does not support dataset: {unsupported[0]}")
        if spec.availability != ModelAvailability.AVAILABLE:
            raise ModelNotRunnableError(spec.unavailable_reason or "model is unavailable")
        if len(references) > spec.resource_limits.max_batch_size:
            raise ModelNotRunnableError("model input exceeds max_batch_size")
        if any(reference.limit > spec.resource_limits.max_input_points for reference in references):
            raise ModelNotRunnableError("model input exceeds max_input_points")
        if any(reference.limit < spec.resource_limits.min_input_points for reference in references):
            raise ModelNotRunnableError(
                "模型输入源点数不足；每条轨迹至少需要 "
                f"{spec.resource_limits.min_input_points} 个源点。"
            )
        return spec


model_registry = ModelRegistry()


__all__ = [
    "DuplicateModelError",
    "ModelNotFoundError",
    "ModelNotRunnableError",
    "ModelRegistry",
    "ModelRegistryError",
    "ModelVersionRequiredError",
    "model_registry",
]
