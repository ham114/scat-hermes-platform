from __future__ import annotations

from typing import Any

import pytest

from agents import experiment_tools, materialization_tools, model_tools
from core.redis_confirmation import RedisConfirmationStore
from scat_model_runtime import DatasetKind, ModelInputReference, ModelTaskRequest, ModelTaskType


class FakeRedisClient:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, str]] = {}

    async def set(self, key: str, value: str, **_options: Any) -> bool:
        if key in self.items:
            return False
        self.items[key] = {"payload": value}
        return True

    async def eval(self, script: str, _count: int, key: str, *args: Any) -> Any:
        if "redis.call('HSET'" in script:
            if key in self.items:
                return 0
            thread, user, payload, _ttl = args
            self.items[key] = {
                "schema_version": "1",
                "thread_id": str(thread),
                "user_id": str(user),
                "payload": str(payload),
            }
            return 1
        item = self.items.get(key)
        if item is None:
            return None if "local payload" in script else 0
        thread, user = args
        if item["thread_id"] != thread or item["user_id"] != user:
            return None if "local payload" in script else 0
        self.items.pop(key)
        return item["payload"] if "local payload" in script else 1


class FakeRuntime:
    enabled = True

    def __init__(self) -> None:
        self.client = FakeRedisClient()

    async def get_client(self) -> FakeRedisClient:
        return self.client

    async def mark_failed(self, _error: BaseException) -> None:
        raise AssertionError("Redis should not fail in this test")


@pytest.mark.asyncio
async def test_all_confirmation_types_round_trip_across_store_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = RedisConfirmationStore(  # type: ignore[arg-type]
        FakeRuntime(), key_prefix="test", max_payload_bytes=262_144
    )
    monkeypatch.setattr(model_tools, "redis_confirmation_store", shared)
    monkeypatch.setattr(materialization_tools, "redis_confirmation_store", shared)
    monkeypatch.setattr(experiment_tools, "redis_confirmation_store", shared)

    model_request = ModelTaskRequest(
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        model_key="test-model",
        input_ref=ModelInputReference(
            dataset=DatasetKind.UAV,
            external_id="flight-1",
            limit=2,
            channels=("cmp_pitch",),
        ),
        parameters={"threshold": 0.5},
    )
    model_writer = model_tools.ModelTaskConfirmationStore(shared=True)
    model_reader = model_tools.ModelTaskConfirmationStore(shared=True)
    model_id, _ = await model_writer.aissue(
        model_request, thread_id="thread-1", user_id="user-1"
    )
    assert (
        await model_reader.aconsume(model_id, thread_id="thread-1", user_id="user-1")
        == model_request
    )

    materialization = materialization_tools.PreparedMaterializationJob(
        kind="flight_features",
        batch="batch-1",
        source_version="source-1",
        algorithm_version="1",
        force=False,
        limit=10,
        total_count=100,
        pending_count=10,
    )
    materialization_writer = materialization_tools.MaterializationJobConfirmationStore(
        shared=True
    )
    materialization_reader = materialization_tools.MaterializationJobConfirmationStore(
        shared=True
    )
    materialization_id, _ = await materialization_writer.aissue(
        materialization, thread_id="thread-1", user_id="user-1"
    )
    assert (
        await materialization_reader.aconsume(
            materialization_id, thread_id="thread-1", user_id="user-1"
        )
        == materialization
    )

    experiment = experiment_tools.PreparedPredictionExperiment(
        batch="batch-1",
        predictor_id="kalman-cv",
        predictor_version="1",
        limit=None,
        stride_s=60.0,
        max_cutoffs_per_flight=12,
        horizons_s=(60.0, 120.0),
        batch_flight_count=100,
    )
    experiment_writer = experiment_tools.ExperimentConfirmationStore(shared=True)
    experiment_reader = experiment_tools.ExperimentConfirmationStore(shared=True)
    experiment_id, _ = await experiment_writer.aissue(
        experiment, thread_id="thread-1", user_id="user-1"
    )
    assert (
        await experiment_reader.aconsume(
            experiment_id, thread_id="thread-1", user_id="user-1"
        )
        == experiment
    )


@pytest.mark.asyncio
async def test_shared_store_unavailable_falls_back_to_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableSharedStore:
        async def issue(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def consume(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def revoke(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

    monkeypatch.setattr(model_tools, "redis_confirmation_store", UnavailableSharedStore())
    store = model_tools.ModelTaskConfirmationStore(shared=True)
    request = ModelTaskRequest(
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        model_key="test-model",
        input_ref=ModelInputReference(
            dataset=DatasetKind.UAV,
            external_id="flight-1",
            limit=2,
            channels=("cmp_pitch",),
        ),
    )

    confirmation_id, _ = await store.aissue(
        request, thread_id="thread-1", user_id="user-1"
    )
    assert (
        await store.aconsume(confirmation_id, thread_id="thread-1", user_id="user-1")
        == request
    )
