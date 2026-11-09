from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from scat.experiments.repository import ExperimentRepository
from scat.materialization.contracts import MaterializationJobRecord
from scat.materialization.repository import MaterializationRepository


@pytest.mark.asyncio
async def test_materialization_job_publishes_after_mongo_write() -> None:
    collection = Mock()
    collection.replace_one = AsyncMock()
    database = Mock()
    database.__getitem__ = Mock(return_value=collection)
    notifier = Mock()
    notifier.publish = AsyncMock()
    repository = MaterializationRepository(database, task_notifier=notifier)
    record = MaterializationJobRecord(
        source_version="source-v1",
        algorithm_version="algorithm-v1",
        generated_at=datetime(2026, 9, 28, tzinfo=UTC),
        job_id="job-1",
        kind="flight_features",
        batch="B",
        status="pending",
    )

    await repository.upsert_job(record)

    collection.replace_one.assert_awaited_once()
    notifier.publish.assert_awaited_once_with(
        domain="materialization",
        task_id="job-1",
        status="pending",
    )


@pytest.mark.asyncio
async def test_experiment_claim_and_fenced_finish_publish_only_committed_states() -> None:
    collection = Mock()
    collection.find_one_and_update = AsyncMock(
        return_value={"experiment_id": "experiment-1", "status": "running"}
    )
    collection.replace_one = AsyncMock(side_effect=[Mock(matched_count=0), Mock(matched_count=1)])
    database = Mock()
    database.__getitem__ = Mock(return_value=collection)
    notifier = Mock()
    notifier.publish = AsyncMock()
    repository = ExperimentRepository(database, task_notifier=notifier)

    claimed = await repository.claim("experiment-1", lease_owner="worker-1")
    assert claimed is not None
    notifier.publish.assert_awaited_once_with(
        domain="experiment",
        task_id="experiment-1",
        status="running",
    )

    terminal = Mock()
    terminal.model_dump.return_value = {
        "experiment_id": "experiment-1",
        "status": "succeeded",
    }
    assert not await repository.finish(terminal, lease_token="old-token")
    assert notifier.publish.await_count == 1
    assert await repository.finish(terminal, lease_token="current-token")
    assert notifier.publish.await_count == 2
    notifier.publish.assert_awaited_with(
        domain="experiment",
        task_id="experiment-1",
        status="succeeded",
    )
