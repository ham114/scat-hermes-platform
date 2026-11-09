"""M9 P0 experiment recovery: requeue, backoff, retry cap, and fencing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scat.experiments import cli
from scat.experiments import runner as runner_module
from scat.experiments.artifacts import ExperimentArtifactStore
from scat.experiments.contracts import ExperimentRecord
from scat.experiments.repository import ExperimentRepository

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


def _repository() -> tuple[ExperimentRepository, MagicMock]:
    database = MagicMock()
    collection = MagicMock()
    database.__getitem__.return_value = collection
    return ExperimentRepository(database), collection


def _document(*, status: str = "running", attempts: int = 1, max_attempts: int = 3) -> dict:
    return {
        "experiment_id": "exp-recovery-1",
        "status": status,
        "attempt_count": attempts,
        "max_attempts": max_attempts,
        "lease_owner": "worker-old",
        "lease_token": "old-token",
        "lease_heartbeat_at": T0 - timedelta(minutes=2),
        "lease_expires_at": T0 - timedelta(minutes=1),
        "message": "worker interrupted",
    }


@pytest.mark.asyncio
async def test_requeue_expired_lease_applies_backoff_and_audit() -> None:
    repository, collection = _repository()
    current = _document()
    requeued = {**current, "status": "pending", "requeue_count": 1}
    collection.find_one = AsyncMock(return_value=current)
    collection.find_one_and_update = AsyncMock(return_value=requeued)

    result = await repository.requeue(
        "exp-recovery-1",
        reason="worker was killed",
        operator="ops-1",
        retry_base_seconds=60,
        retry_max_seconds=600,
        now=T0,
    )

    assert result["outcome"] == "requeued"
    filters, update = collection.find_one_and_update.await_args.args
    assert filters == {
        "experiment_id": "exp-recovery-1",
        "status": "running",
        "attempt_count": 1,
        "lease_token": "old-token",
        "lease_expires_at": {"$lte": T0},
    }
    assert update["$set"]["status"] == "pending"
    assert update["$set"]["next_attempt_at"] == T0 + timedelta(seconds=60)
    assert update["$set"]["lease_token"] is None
    assert update["$inc"] == {"requeue_count": 1}
    event = update["$push"]["requeue_history"]["$each"][0]
    assert event["operator"] == "ops-1"
    assert event["reason"] == "worker was killed"
    assert event["outcome"] == "requeued"
    assert "lease_token" not in event


@pytest.mark.asyncio
async def test_requeue_refuses_live_lease() -> None:
    repository, collection = _repository()
    current = _document()
    current["lease_expires_at"] = T0 + timedelta(minutes=1)
    collection.find_one = AsyncMock(return_value=current)
    collection.find_one_and_update = AsyncMock()

    result = await repository.requeue(
        "exp-recovery-1",
        reason="operator requested",
        operator="ops-1",
        now=T0,
    )

    assert result["outcome"] == "active_lease"
    collection.find_one_and_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_exhausted_attempts_sets_terminal_failed() -> None:
    repository, collection = _repository()
    current = _document(attempts=3, max_attempts=3)
    exhausted = {**current, "status": "failed", "lease_token": None}
    collection.find_one = AsyncMock(return_value=current)
    collection.find_one_and_update = AsyncMock(return_value=exhausted)

    result = await repository.requeue(
        "exp-recovery-1",
        reason="third worker failed",
        operator="ops-1",
        now=T0,
    )

    assert result["outcome"] == "retry_exhausted"
    update = collection.find_one_and_update.await_args.args[1]
    assert update["$set"]["status"] == "failed"
    assert update["$set"]["finished_at"] == T0
    assert update["$set"]["next_attempt_at"] is None
    assert "3/3" in update["$set"]["message"]
    event = update["$push"]["requeue_history"]["$each"][0]
    assert event["outcome"] == "retry_exhausted"


@pytest.mark.asyncio
async def test_finish_is_fenced_by_current_lease_token() -> None:
    repository, collection = _repository()
    collection.replace_one = AsyncMock(
        side_effect=[SimpleNamespace(matched_count=1), SimpleNamespace(matched_count=0)]
    )
    record = ExperimentRecord(
        experiment_id="exp-recovery-1",
        status="succeeded",
        batch="b",
        predictor_id="kalman-cv",
        predictor_version="1",
        model_version="1",
        data_version="d",
        feature_version="f",
        lease_owner="worker-new",
        lease_token="new-token-value-123",
        attempt_count=2,
        code_version="test",
        created_at=T0,
        finished_at=T0,
    )

    assert await repository.finish(record, lease_token="new-token-value-123")
    assert not await repository.finish(record, lease_token="stale-token-value")
    first_filter = collection.replace_one.await_args_list[0].args[0]
    second_filter = collection.replace_one.await_args_list[1].args[0]
    assert first_filter["lease_token"] == "new-token-value-123"
    assert second_filter["lease_token"] == "stale-token-value"
    assert first_filter["status"] == second_filter["status"] == "running"
    assert collection.replace_one.await_args_list[0].kwargs["upsert"] is False


def test_retry_attempts_write_reports_to_fenced_paths(tmp_path) -> None:
    first = ExperimentRecord(
        experiment_id="exp-recovery-1",
        status="succeeded",
        batch="b",
        predictor_id="kalman-cv",
        predictor_version="1",
        model_version="1",
        data_version="d",
        feature_version="f",
        lease_owner="worker-old",
        lease_token="old-token-value-123",
        attempt_count=1,
        code_version="test",
        created_at=T0,
        finished_at=T0,
    )
    second = first.model_copy(
        update={
            "lease_owner": "worker-new",
            "lease_token": "new-token-value-456",
            "attempt_count": 2,
        },
        deep=True,
    )
    store = ExperimentArtifactStore(tmp_path)

    runner_module._register_report_artifact(first, store)
    runner_module._register_report_artifact(second, store)

    first_path = first.artifacts[0]["path"]
    second_path = second.artifacts[0]["path"]
    assert first_path != second_path
    assert ".attempt-1." in first_path
    assert ".attempt-2." in second_path
    assert (tmp_path / first_path).is_file()
    assert (tmp_path / second_path).is_file()


def test_requeue_cli_requires_operator_and_reason() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "requeue",
            "--experiment-id",
            "exp-recovery-1",
            "--operator",
            "ops-1",
            "--reason",
            "worker interrupted",
        ]
    )

    assert args.command == "requeue"
    assert args.experiment_id == "exp-recovery-1"
    assert args.operator == "ops-1"
    assert args.reason == "worker interrupted"


@pytest.mark.asyncio
async def test_requeue_cli_calls_repository_with_audit_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "requeue",
            "--experiment-id",
            "exp-recovery-1",
            "--operator",
            "ops-1",
            "--reason",
            "worker interrupted",
        ]
    )
    repository = MagicMock()
    repository.requeue = AsyncMock(
        return_value={
            "outcome": "requeued",
            "document": {
                "status": "pending",
                "attempt_count": 1,
                "max_attempts": 3,
                "next_attempt_at": T0,
                "requeue_count": 1,
            },
        }
    )
    fake_database = SimpleNamespace(db=object(), connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(cli, "flight_database", fake_database)
    monkeypatch.setattr(cli, "ExperimentRepository", lambda _database: repository)

    result = await cli._execute(args)

    assert result["status"] == "succeeded"
    assert result["outcome"] == "requeued"
    assert result["experiment_status"] == "pending"
    repository.requeue.assert_awaited_once()
    kwargs = repository.requeue.await_args.kwargs
    assert kwargs["reason"] == "worker interrupted"
    assert kwargs["operator"] == "ops-1"
    fake_database.connect.assert_awaited_once()
    fake_database.close.assert_awaited_once()
