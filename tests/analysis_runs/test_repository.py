"""Atomic repository tests without a live MongoDB."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analysis_runs import (
    AnalysisRunComponents,
    AnalysisRunConflictError,
    AnalysisRunError,
    AnalysisRunRecord,
    AnalysisRunRepository,
    AnalysisRunStatusEvent,
)
from scat_model_runtime import (
    DatasetKind,
    ModelExecutionMode,
    ModelInputReference,
    ModelRunStatus,
    ModelTaskType,
)


def _record() -> AnalysisRunRecord:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    return AnalysisRunRecord(
        run_id="b" * 32,
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
            ),
        ),
        components=AnalysisRunComponents(),
        code_version="test",
        status=ModelRunStatus.QUEUED,
        progress=0.0,
        status_history=(
            AnalysisRunStatusEvent(status=ModelRunStatus.QUEUED, progress=0.0, at=now),
        ),
        created_at=now,
        updated_at=now,
        queued_at=now,
    )


class _Cursor:
    def __init__(self, documents):
        self.documents = documents
        self.sort_args = None
        self.limit_value = None

    def sort(self, *args):
        self.sort_args = args
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    async def to_list(self, *, length):
        return self.documents[:length]


@pytest.mark.asyncio
async def test_repository_creates_indexes_and_atomically_transitions():
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        insert_one=AsyncMock(),
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)),
    )
    repository = AnalysisRunRepository(collection=collection)
    record = _record()
    finished_at = datetime(2026, 9, 16, 0, 1, tzinfo=UTC)

    await repository.create(record)
    await repository.transition(
        record.run_id,
        expected_statuses=(ModelRunStatus.RUNNING,),
        status=ModelRunStatus.FAILED,
        progress=1.0,
        at=finished_at,
        message="failed",
        error=AnalysisRunError(code="model_execution_failed", message="safe failure"),
    )

    collection.create_indexes.assert_awaited_once()
    collection.insert_one.assert_awaited_once()
    query, update = collection.update_one.await_args.args
    assert query == {
        "run_id": record.run_id,
        "status": {"$in": ["running"]},
    }
    assert update["$set"]["status"] == "failed"
    assert update["$set"]["finished_at"] == finished_at
    assert update["$set"]["error"]["code"] == "model_execution_failed"
    assert update["$push"]["status_history"]["status"] == ModelRunStatus.FAILED


@pytest.mark.asyncio
async def test_repository_rejects_stale_transition():
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=0)),
    )
    repository = AnalysisRunRepository(collection=collection)

    with pytest.raises(AnalysisRunConflictError):
        await repository.transition(
            "b" * 32,
            expected_statuses=(ModelRunStatus.QUEUED,),
            status=ModelRunStatus.RUNNING,
            progress=0.05,
        )


@pytest.mark.asyncio
async def test_repository_reads_only_with_owner_filter():
    record = _record()
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        find_one=AsyncMock(return_value=record.to_mongo_document()),
    )
    repository = AnalysisRunRepository(collection=collection)

    restored = await repository.get_for_owner(record.run_id, owner_user_id="user-1")

    assert restored == record
    collection.find_one.assert_awaited_once_with(
        {"run_id": record.run_id, "owner_user_id": "user-1"}
    )


@pytest.mark.asyncio
async def test_repository_lists_recent_runs_with_owner_and_filters():
    record = _record()
    cursor = _Cursor([record.to_mongo_document()])
    collection = SimpleNamespace(create_indexes=AsyncMock(), find=lambda query: cursor)
    repository = AnalysisRunRepository(collection=collection)

    restored = await repository.list_for_owner(
        owner_user_id="user-1",
        model_key="scat-gbar-v23",
        status=ModelRunStatus.QUEUED,
        limit=10,
    )

    assert restored == [record]
    assert cursor.limit_value == 10
