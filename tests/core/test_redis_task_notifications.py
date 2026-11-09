from __future__ import annotations

from typing import Any

import pytest

from core.redis_task_notifications import RedisTaskNotifier


class FakeRedisClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, str]]] = []

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> str:
        assert key == "test:task_events"
        assert maxlen == 100 and approximate is True
        stream_id = f"{len(self.messages) + 1}-0"
        self.messages.append((stream_id, fields))
        return stream_id

    async def xread(
        self,
        streams: dict[str, str],
        *,
        count: int,
        block: int | None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        assert count <= 500 and block is None
        last_id = streams["test:task_events"]
        rows = [item for item in self.messages if item[0] > last_id][:count]
        return [("test:task_events", rows)] if rows else []


class FakeRuntime:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.enabled = True
        self.failures: list[BaseException] = []

    async def get_client(self) -> Any:
        return self.client

    async def mark_failed(self, error: BaseException) -> None:
        self.failures.append(error)


@pytest.mark.asyncio
async def test_stream_notification_is_bounded_metadata_and_replayable() -> None:
    client = FakeRedisClient()
    notifier = RedisTaskNotifier(
        FakeRuntime(client),  # type: ignore[arg-type]
        key_prefix="test",
        maxlen=100,
    )

    stream_id = await notifier.publish(
        domain="experiment",
        task_id="experiment-1",
        status="pending",
    )
    assert stream_id == "1-0"
    first = await notifier.read(last_id="0-0")
    duplicate = await notifier.read(last_id="0-0")
    assert first == duplicate
    assert first[0].task_id == "experiment-1"
    assert set(client.messages[0][1]) == {
        "schema_version",
        "domain",
        "task_id",
        "status",
        "emitted_at",
    }


@pytest.mark.asyncio
async def test_stream_notification_rejects_unsafe_metadata_and_fails_open() -> None:
    class FailingClient:
        async def xadd(self, *_args: Any, **_kwargs: Any) -> str:
            raise TimeoutError("offline")

    runtime = FakeRuntime(FailingClient())
    notifier = RedisTaskNotifier(runtime, key_prefix="test", maxlen=100)  # type: ignore[arg-type]
    assert (
        await notifier.publish(domain="materialization", task_id="job-1", status="pending")
        is None
    )
    assert len(runtime.failures) == 1
    with pytest.raises(ValueError, match="invalid task notification ID"):
        await notifier.publish(
            domain="experiment",
            task_id="contains a secret payload",
            status="pending",
        )
