"""Best-effort Redis Stream notifications for MongoDB-authoritative tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from core.redis_runtime import RedisRuntime, redis_runtime
from core.settings import settings

TaskDomain = Literal["materialization", "experiment"]

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_STATUS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_STREAM_ID = re.compile(r"^(?:\$|\d+-\d+)$")


@dataclass(frozen=True, slots=True)
class RedisTaskEvent:
    stream_id: str
    domain: TaskDomain
    task_id: str
    status: str
    emitted_at: str


class RedisTaskNotifier:
    """Publishes bounded wake-up hints; consumers must re-read MongoDB state."""

    def __init__(
        self,
        runtime: RedisRuntime,
        *,
        key_prefix: str,
        maxlen: int,
    ) -> None:
        if maxlen < 1:
            raise ValueError("task event maxlen must be positive")
        self._runtime = runtime
        self.stream_key = f"{key_prefix}:task_events"
        self.maxlen = int(maxlen)

    @property
    def enabled(self) -> bool:
        return self._runtime.enabled

    async def publish(self, *, domain: TaskDomain, task_id: str, status: str) -> str | None:
        if domain not in {"materialization", "experiment"}:
            raise ValueError("unknown task notification domain")
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task notification ID")
        if not _STATUS.fullmatch(status):
            raise ValueError("invalid task notification status")
        client = await self._runtime.get_client()
        if client is None:
            return None
        try:
            result = await client.xadd(
                self.stream_key,
                {
                    "schema_version": "1",
                    "domain": domain,
                    "task_id": task_id,
                    "status": status,
                    "emitted_at": datetime.now(UTC).isoformat(),
                },
                maxlen=self.maxlen,
                approximate=True,
            )
            return str(result)
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return None

    async def read(
        self,
        *,
        last_id: str,
        count: int = 100,
        block_ms: int = 0,
    ) -> list[RedisTaskEvent]:
        """Read wake-up hints without acknowledging or deleting them."""
        if not _STREAM_ID.fullmatch(last_id):
            raise ValueError("invalid Redis Stream ID")
        safe_count = max(1, min(int(count), 500))
        safe_block_ms = max(0, min(int(block_ms), 250))
        client = await self._runtime.get_client()
        if client is None:
            return []
        try:
            response = await client.xread(
                {self.stream_key: last_id},
                count=safe_count,
                block=safe_block_ms or None,
            )
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return []
        events: list[RedisTaskEvent] = []
        for _stream, messages in response or []:
            for stream_id, fields in messages:
                event = _parse_event(str(stream_id), fields)
                if event is not None:
                    events.append(event)
        return events


def _parse_event(stream_id: str, fields: Any) -> RedisTaskEvent | None:
    if not isinstance(fields, dict) or fields.get("schema_version") != "1":
        return None
    domain = fields.get("domain")
    task_id = fields.get("task_id")
    status = fields.get("status")
    emitted_at = fields.get("emitted_at")
    if domain not in {"materialization", "experiment"}:
        return None
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        return None
    if not isinstance(status, str) or not _STATUS.fullmatch(status):
        return None
    if not isinstance(emitted_at, str):
        return None
    return RedisTaskEvent(
        stream_id=stream_id,
        domain=domain,
        task_id=task_id,
        status=status,
        emitted_at=emitted_at,
    )


redis_task_notifier = RedisTaskNotifier(
    redis_runtime,
    key_prefix=settings.REDIS_KEY_PREFIX,
    maxlen=settings.REDIS_TASK_EVENT_MAXLEN,
)


__all__ = ["RedisTaskEvent", "RedisTaskNotifier", "redis_task_notifier"]
