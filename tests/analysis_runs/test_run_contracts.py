"""Contract tests for persistent model analysis run documents."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from analysis_runs import (
    AnalysisRunComponents,
    AnalysisRunError,
    AnalysisRunRecord,
    AnalysisRunStatusEvent,
)
from scat_model_runtime import (
    DatasetKind,
    ModelExecutionMode,
    ModelInputReference,
    ModelRunStatus,
    ModelTaskType,
)


def _record(*, status: ModelRunStatus = ModelRunStatus.QUEUED) -> AnalysisRunRecord:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    return AnalysisRunRecord(
        run_id="a" * 32,
        owner_user_id="user-1",
        thread_id="thread-1",
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        execution_mode=ModelExecutionMode.SYNC,
        model_key="scat-gbar-v23",
        model_version="23",
        input_schema="regression-task-input/1",
        output_schema="regression-task-output/1",
        input_refs=(
            ModelInputReference(
                dataset=DatasetKind.SCAT,
                batch="batch",
                external_id="100002",
                limit=300,
            ),
        ),
        components=AnalysisRunComponents(),
        code_version="bc6654d",
        status=status,
        progress=0.0,
        status_history=(AnalysisRunStatusEvent(status=status, progress=0.0, at=now),),
        created_at=now,
        updated_at=now,
        queued_at=now,
        error=(
            AnalysisRunError(code="test_failure", message="safe")
            if status == ModelRunStatus.FAILED
            else None
        ),
    )


def test_analysis_run_round_trips_mongo_document_with_datetime_values():
    record = _record()

    document = record.to_mongo_document()
    # PyMongo's default BSON decoder returns UTC datetimes without tzinfo.
    document["created_at"] = document["created_at"].replace(tzinfo=None)
    document["updated_at"] = document["updated_at"].replace(tzinfo=None)
    document["queued_at"] = document["queued_at"].replace(tzinfo=None)
    document["status_history"][0]["at"] = document["status_history"][0]["at"].replace(tzinfo=None)
    restored = AnalysisRunRecord.from_mongo_document({"_id": "ignored", **document})

    assert isinstance(document["created_at"], datetime)
    assert restored == record


def test_exact_input_window_preserves_timezone_and_microseconds_through_real_bson():
    from bson import BSON

    start = datetime(2016, 10, 20, 19, 57, 27, 593750, tzinfo=UTC)
    end = datetime(2016, 10, 20, 20, 14, 3, 398437, tzinfo=UTC)
    original = _record()
    reference = original.input_refs[0].model_copy(
        update={"start_time": start, "end_time": end, "limit": 200}
    )
    record = original.model_copy(update={"input_refs": (reference,)})
    encoded = record.to_mongo_document()
    assert isinstance(encoded["created_at"], datetime)
    assert isinstance(encoded["input_refs"][0]["start_time"], str)
    decoded = BSON(BSON.encode(encoded)).decode()
    restored = AnalysisRunRecord.from_mongo_document(decoded)
    assert restored.input_refs[0].start_time == start
    assert restored.input_refs[0].end_time == end
    assert restored.input_refs[0].limit == 200


def test_legacy_mongo_input_dates_are_utc_without_relaxing_external_contract():
    from bson import BSON

    original = _record()
    value = original.model_dump(mode="python", exclude_none=True)
    value["input_refs"][0].update(
        start_time=datetime(2020, 1, 1, tzinfo=UTC), end_time=datetime(2020, 1, 2, tzinfo=UTC)
    )
    decoded = BSON(BSON.encode(value)).decode()
    restored = AnalysisRunRecord.from_mongo_document(decoded)
    assert restored.input_refs[0].end_time == datetime(2020, 1, 2, tzinfo=UTC)
    assert decoded["input_refs"][0]["end_time"].tzinfo is None
    with pytest.raises(ValidationError, match="timezone"):
        ModelInputReference.model_validate(decoded["input_refs"][0])


def test_failed_run_requires_safe_error():
    record = _record()

    with pytest.raises(ValidationError, match="failed analysis runs require an error"):
        record.model_copy(
            update={"status": ModelRunStatus.FAILED},
        ).__class__.model_validate(
            {
                **record.model_dump(mode="python"),
                "status": ModelRunStatus.FAILED,
                "status_history": (
                    AnalysisRunStatusEvent(
                        status=ModelRunStatus.FAILED,
                        progress=0.0,
                        at=record.updated_at,
                    ),
                ),
            }
        )
