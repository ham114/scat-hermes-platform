"""Tests for model-agent boundary contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from scat_model_runtime import (
    DatasetKind,
    ModelAvailability,
    ModelExecutionMode,
    ModelInputReference,
    ModelResourceLimits,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
)


def _limits() -> ModelResourceLimits:
    return ModelResourceLimits(
        max_input_points=500,
        max_output_points=200,
        timeout_seconds=30,
    )


def test_model_spec_requires_explicit_unavailable_reason():
    with pytest.raises(ValidationError, match="unavailable_reason"):
        ModelSpec(
            model_key="gbar-trajectory",
            version="0.1.0",
            display_name="GBAR",
            description="Trajectory prediction model.",
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            supported_datasets={DatasetKind.SCAT},
            input_schema="trajectory-window/v1",
            output_schema="analysis-result/prediction-v1",
            execution_mode=ModelExecutionMode.ASYNC,
            resource_limits=_limits(),
        )


def test_model_input_reference_enforces_dataset_identity_and_time_bounds():
    scat = ModelInputReference(
        dataset=DatasetKind.SCAT,
        batch="week1",
        external_id="1394",
        start_time=datetime(2016, 10, 20, 20, tzinfo=UTC),
        end_time=datetime(2016, 10, 20, 21, tzinfo=UTC),
        fields=("position", "motion"),
    )
    assert scat.batch == "week1"

    with pytest.raises(ValidationError, match="requires batch"):
        ModelInputReference(dataset=DatasetKind.SCAT, external_id="1394")

    with pytest.raises(ValidationError, match="does not accept batch"):
        ModelInputReference(
            dataset=DatasetKind.UAV,
            batch="not-valid-for-uav",
            external_id="flight-1",
        )

    with pytest.raises(ValidationError, match="include a timezone"):
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="week1",
            external_id="1394",
            start_time=datetime(2016, 10, 20, 20),
        )


def test_model_task_request_bounds_free_form_parameters():
    input_ref = ModelInputReference(
        dataset=DatasetKind.UAV,
        external_id="flight-1",
        channels=("cmp_comb_alt", "cmp_ground_speed"),
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        model_key="sensor-classifier",
        input_ref=input_ref,
        parameters={"threshold": 0.75, "labels": ["normal", "anomaly"]},
        confirmed=True,
    )
    assert request.confirmed is True

    with pytest.raises(ValidationError, match="cannot exceed 100 items"):
        ModelTaskRequest(
            task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
            model_key="sensor-classifier",
            input_ref=input_ref,
            parameters={"oversized": list(range(101))},
        )


def test_model_task_request_accepts_one_unambiguous_batch_reference_list():
    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch="week1",
            external_id=str(external_id),
        )
        for external_id in (100001, 100002)
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="gbar",
        input_refs=references,
    )

    assert request.references == references
    assert request.primary_input_ref == references[0]

    with pytest.raises(ValidationError, match="exactly one"):
        ModelTaskRequest(
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            model_key="gbar",
            input_ref=references[0],
            input_refs=references,
        )

    with pytest.raises(ValidationError, match="must be unique"):
        ModelTaskRequest(
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            model_key="gbar",
            input_refs=(references[0], references[0]),
        )


def test_available_model_cannot_keep_an_unavailable_reason():
    with pytest.raises(ValidationError, match="cannot have unavailable_reason"):
        ModelSpec(
            model_key="baseline",
            version="1.0.0",
            display_name="Baseline",
            description="Deterministic baseline.",
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            supported_datasets={DatasetKind.SCAT},
            input_schema="trajectory-window/v1",
            output_schema="analysis-result/prediction-v1",
            execution_mode=ModelExecutionMode.SYNC,
            resource_limits=_limits(),
            availability=ModelAvailability.AVAILABLE,
            unavailable_reason="stale reason",
        )
