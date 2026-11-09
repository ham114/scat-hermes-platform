"""Tests for the explicit task-model allowlist."""

import pytest

from scat_model_runtime import (
    DatasetKind,
    ModelAvailability,
    ModelExecutionMode,
    ModelInputReference,
    ModelRegistry,
    ModelResourceLimits,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
)
from scat_model_runtime.registry import (
    DuplicateModelError,
    ModelNotFoundError,
    ModelNotRunnableError,
    ModelVersionRequiredError,
)


def _spec(
    *,
    version: str = "1.0.0",
    task_type: ModelTaskType = ModelTaskType.TRAJECTORY_PREDICTION,
    datasets: frozenset[DatasetKind] = frozenset({DatasetKind.SCAT}),
    available: bool = True,
    is_default: bool = False,
) -> ModelSpec:
    return ModelSpec(
        model_key="trajectory-baseline",
        version=version,
        display_name="Trajectory baseline",
        description="Test-only deterministic trajectory baseline.",
        task_type=task_type,
        supported_datasets=datasets,
        input_schema="trajectory-window/v1",
        output_schema="analysis-result/prediction-v1",
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=500,
            max_output_points=200,
            timeout_seconds=10,
        ),
        availability=(ModelAvailability.AVAILABLE if available else ModelAvailability.UNAVAILABLE),
        unavailable_reason=None if available else "adapter is not installed",
        is_default=is_default,
    )


def _request(
    *,
    dataset: DatasetKind = DatasetKind.SCAT,
    task_type: ModelTaskType = ModelTaskType.TRAJECTORY_PREDICTION,
    limit: int = 200,
) -> ModelTaskRequest:
    return ModelTaskRequest(
        task_type=task_type,
        model_key="trajectory-baseline",
        input_ref=ModelInputReference(
            dataset=dataset,
            batch="week1" if dataset == DatasetKind.SCAT else None,
            external_id="1394" if dataset == DatasetKind.SCAT else "flight-1",
            limit=limit,
        ),
        confirmed=True,
    )


def test_registry_rejects_duplicate_model_and_default_versions():
    registry = ModelRegistry([_spec(is_default=True)])

    with pytest.raises(DuplicateModelError, match="already registered"):
        registry.register(_spec(is_default=True))

    with pytest.raises(DuplicateModelError, match="default model version"):
        registry.register(_spec(version="2.0.0", is_default=True))


def test_registry_requires_version_when_multiple_versions_have_no_default():
    registry = ModelRegistry([_spec(), _spec(version="2.0.0")])

    with pytest.raises(ModelVersionRequiredError):
        registry.get("trajectory-baseline")
    assert registry.get("trajectory-baseline", "2.0.0").version == "2.0.0"
    with pytest.raises(ModelNotFoundError):
        registry.get("missing")


def test_registry_filters_and_resolves_compatible_model():
    registry = ModelRegistry(
        [
            _spec(is_default=True),
            _spec(version="2.0.0", available=False),
        ]
    )

    listed = registry.list(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        dataset=DatasetKind.SCAT,
        availability=ModelAvailability.AVAILABLE,
    )
    assert [spec.registry_id for spec in listed] == ["trajectory-baseline:1.0.0"]
    assert registry.resolve(_request()).version == "1.0.0"


@pytest.mark.parametrize(
    ("task_request", "message"),
    [
        (
            _request(dataset=DatasetKind.UAV),
            "does not support dataset",
        ),
        (
            _request(task_type=ModelTaskType.MODEL_EVALUATION),
            "task mismatch",
        ),
        (
            _request(limit=501),
            "exceeds max_input_points",
        ),
    ],
)
def test_registry_rejects_incompatible_requests(task_request, message):
    registry = ModelRegistry([_spec(is_default=True)])

    with pytest.raises(ModelNotRunnableError, match=message):
        registry.resolve(task_request)


def test_registry_enforces_minimum_points_and_batch_size():
    spec = _spec().model_copy(
        update={
            "resource_limits": ModelResourceLimits(
                min_input_points=71,
                max_input_points=500,
                max_output_points=200,
                timeout_seconds=10,
                max_batch_size=2,
            )
        }
    )
    registry = ModelRegistry([spec])
    too_short = _request(limit=36)
    with pytest.raises(ModelNotRunnableError, match="至少需要 71"):
        registry.resolve(too_short)

    references = tuple(
        _request().primary_input_ref.model_copy(update={"external_id": str(index)})
        for index in range(3)
    )
    too_many = _request().model_copy(update={"input_ref": None, "input_refs": references})
    with pytest.raises(ModelNotRunnableError, match="max_batch_size"):
        registry.resolve(too_many)


def test_registry_preserves_unavailable_reason_without_running_model():
    registry = ModelRegistry([_spec(available=False)])

    with pytest.raises(ModelNotRunnableError, match="adapter is not installed"):
        registry.resolve(_request())


def test_registry_refuses_unconfirmed_execution_request():
    registry = ModelRegistry([_spec(is_default=True)])
    task_request = _request().model_copy(update={"confirmed": False})

    with pytest.raises(ModelNotRunnableError, match="has not been confirmed"):
        registry.resolve(task_request)
