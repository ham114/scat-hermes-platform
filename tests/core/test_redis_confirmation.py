from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from core.redis_confirmation import RedisConfirmationStore
from core.redis_runtime import RedisRuntime


class FakeRedisClient:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, str]] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool:
        assert ex > 0 and nx is True
        if key in self.items:
            return False
        self.items[key] = {"payload": value}
        return True

    async def eval(self, script: str, _key_count: int, key: str, *args: Any) -> Any:
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
        if "local payload" in script:
            return item["payload"]
        return 1


class FakeRuntime:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.failures: list[BaseException] = []
        self.enabled = True

    async def get_client(self) -> Any:
        return self.client

    async def mark_failed(self, error: BaseException) -> None:
        self.failures.append(error)


@pytest.mark.asyncio
async def test_redis_confirmation_is_scope_bound_single_use_and_hides_raw_id() -> None:
    client = FakeRedisClient()
    runtime = FakeRuntime(client)
    store = RedisConfirmationStore(runtime, key_prefix="test", max_payload_bytes=4096)  # type: ignore[arg-type]

    issued = await store.issue(
        "model",
        {"model_key": "gbar"},
        ttl=timedelta(minutes=15),
        thread_id="thread-1",
        user_id="user-1",
    )

    assert issued is not None
    confirmation_id, _expires_at = issued
    assert all(confirmation_id not in key for key in client.items)
    assert (
        await store.consume(
            "model",
            confirmation_id,
            thread_id="other-thread",
            user_id="user-1",
        )
        is None
    )
    assert await store.consume(
        "model",
        confirmation_id,
        thread_id="thread-1",
        user_id="user-1",
    ) == {"model_key": "gbar"}
    assert (
        await store.consume(
            "model",
            confirmation_id,
            thread_id="thread-1",
            user_id="user-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_redis_confirmation_revoke_is_scope_bound() -> None:
    client = FakeRedisClient()
    runtime = FakeRuntime(client)
    store = RedisConfirmationStore(runtime, key_prefix="test", max_payload_bytes=4096)  # type: ignore[arg-type]
    issued = await store.issue(
        "experiment",
        {"batch": "batch-1"},
        ttl=timedelta(minutes=15),
        thread_id="thread-1",
        user_id="user-1",
    )
    assert issued is not None
    confirmation_id, _expires_at = issued

    assert not await store.revoke(
        "experiment", confirmation_id, thread_id="thread-2", user_id="user-1"
    )
    assert await store.revoke(
        "experiment", confirmation_id, thread_id="thread-1", user_id="user-1"
    )


@pytest.mark.asyncio
async def test_redis_failure_is_safe_and_disabled_runtime_is_explicit() -> None:
    class FailingClient:
        async def eval(self, *_args: Any, **_kwargs: Any) -> bool:
            raise TimeoutError("offline")

    runtime = FakeRuntime(FailingClient())
    store = RedisConfirmationStore(runtime, key_prefix="test", max_payload_bytes=4096)  # type: ignore[arg-type]
    result = await store.issue(
        "model",
        {"model_key": "gbar"},
        ttl=timedelta(minutes=15),
        thread_id=None,
        user_id=None,
    )

    assert result is None
    assert len(runtime.failures) == 1
    disabled = RedisRuntime(url=None)
    assert await disabled.status() == "disabled"
    await disabled.close()
