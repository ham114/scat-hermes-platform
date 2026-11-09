"""One owned synthetic Mongo round trip in the final platform, with mandatory cleanup."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from analysis_runs import AnalysisRunComponents, AnalysisRunRecord, AnalysisRunStatusEvent
from analysis_runs.repository import analysis_run_repository as repo
from scat_model_runtime import (
    ModelExecutionMode,
    ModelInputReference,
    ModelRunStatus,
    ModelTaskType,
)


async def main():
    identity = uuid4().hex
    owner = "forecast-storage-probe-" + identity
    now = datetime.now(UTC).replace(microsecond=0)
    start = datetime(2020, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    end = datetime(2020, 1, 1, 0, 10, 0, 654321, tzinfo=UTC)
    condition = {
        "run_id": identity,
        "owner_user_id": owner,
        "thread_id": identity,
        "model_key": "storage-contract-probe",
    }
    await repo.ensure_indexes()
    assert await repo.collection.find_one(condition) is None
    try:
        record = AnalysisRunRecord(
            **condition,
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            execution_mode=ModelExecutionMode.ASYNC,
            model_version="1",
            input_schema="workflow-input/v1",
            output_schema="AnalysisResult/1.6",
            input_refs=(
                ModelInputReference(
                    dataset="scat",
                    batch="SYNTH-CONTRACT",
                    external_id="synthetic-tail",
                    limit=200,
                    start_time=start,
                    end_time=end,
                ),
            ),
            components=AnalysisRunComponents(),
            code_version="synthetic-storage-probe",
            status=ModelRunStatus.QUEUED,
            progress=0,
            status_history=(
                AnalysisRunStatusEvent(status=ModelRunStatus.QUEUED, progress=0, at=now),
            ),
            created_at=now,
            updated_at=now,
            queued_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        await repo.create(record)
        restored = await repo.get_for_owner(identity, owner_user_id=owner)
        assert restored.input_refs[0].start_time == start and restored.input_refs[0].end_time == end
        raw = await repo.collection.find_one(condition)
        assert isinstance(raw["input_refs"][0]["start_time"], str)
        assert isinstance(raw["expires_at"], datetime)
        assert await repo.get_for_owner(identity, owner_user_id=owner + "-other") is None
    finally:
        removed = await repo.collection.delete_one(condition)
        assert removed.deleted_count == 1
        assert await repo.collection.count_documents(condition) == 0
    print(
        json.dumps(
            {
                "status": "passed",
                "actual_mongo_driver": True,
                "exact_microseconds_and_timezone_preserved": True,
                "ttl_datetime_preserved": True,
                "wrong_owner_rejected": True,
                "owned_synthetic_record_removed": True,
                "llm_calls": 0,
                "prediction_calls": 0,
            }
        )
    )


asyncio.run(main())
