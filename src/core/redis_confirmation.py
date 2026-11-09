"""Atomic Redis storage for short-lived, single-use confirmation payloads."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from core.redis_runtime import RedisRuntime, redis_runtime
from core.settings import settings

logger = logging.getLogger(__name__)

_SAFE_NAMESPACE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONFIRMATION_ID = re.compile(r"^[0-9a-f]{32}$")

_ISSUE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1],
  'schema_version', '1',
  'thread_id', ARGV[1],
  'user_id', ARGV[2],
  'payload', ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return 1
"""

_CONSUME_SCRIPT = """
if redis.call('HGET', KEYS[1], 'schema_version') ~= '1' then return false end
if redis.call('HGET', KEYS[1], 'thread_id') ~= ARGV[1] then return false end
if redis.call('HGET', KEYS[1], 'user_id') ~= ARGV[2] then return false end
local payload = redis.call('HGET', KEYS[1], 'payload')
redis.call('DEL', KEYS[1])
return payload
"""

_REVOKE_SCRIPT = """
if redis.call('HGET', KEYS[1], 'schema_version') ~= '1' then return 0 end
if redis.call('HGET', KEYS[1], 'thread_id') ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[1], 'user_id') ~= ARGV[2] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class RedisConfirmationStore:
    """Stores only prepared request payloads; MongoDB remains the task ledger."""

    def __init__(
        self,
        runtime: RedisRuntime,
        *,
        key_prefix: str,
        max_payload_bytes: int,
    ) -> None:
        self._runtime = runtime
        self._key_prefix = key_prefix
        self._max_payload_bytes = int(max_payload_bytes)

    @property
    def enabled(self) -> bool:
        return self._runtime.enabled

    def _key(self, namespace: str, confirmation_id: str) -> str:
        if not _SAFE_NAMESPACE.fullmatch(namespace):
            raise ValueError("unsafe confirmation namespace")
        if not _CONFIRMATION_ID.fullmatch(confirmation_id):
            raise ValueError("invalid confirmation ID")
        digest = hashlib.sha256(confirmation_id.encode("ascii")).hexdigest()
        return f"{self._key_prefix}:confirmation:{namespace}:{digest}"

    async def issue(
        self,
        namespace: str,
        payload: dict[str, Any],
        *,
        ttl: timedelta,
        thread_id: str | None,
        user_id: str | None,
    ) -> tuple[str, datetime] | None:
        client = await self._runtime.get_client()
        if client is None:
            return None
        ttl_seconds = max(1, math.ceil(ttl.total_seconds()))
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > self._max_payload_bytes:
            logger.warning("Redis confirmation payload exceeded configured byte limit")
            return None
        try:
            for _attempt in range(3):
                confirmation_id = uuid4().hex
                stored = await client.eval(
                    _ISSUE_SCRIPT,
                    1,
                    self._key(namespace, confirmation_id),
                    thread_id or "",
                    user_id or "",
                    encoded,
                    ttl_seconds,
                )
                if int(stored or 0) == 1:
                    return confirmation_id, expires_at
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return None
        raise RuntimeError("could not allocate a unique confirmation ID")

    async def consume(
        self,
        namespace: str,
        confirmation_id: str,
        *,
        thread_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any] | None:
        client = await self._runtime.get_client()
        if client is None or not _CONFIRMATION_ID.fullmatch(confirmation_id):
            return None
        try:
            raw = await client.eval(
                _CONSUME_SCRIPT,
                1,
                self._key(namespace, confirmation_id),
                thread_id or "",
                user_id or "",
            )
            if not isinstance(raw, str):
                return None
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return None

    async def revoke(
        self,
        namespace: str,
        confirmation_id: str,
        *,
        thread_id: str | None,
        user_id: str | None,
    ) -> bool:
        client = await self._runtime.get_client()
        if client is None or not _CONFIRMATION_ID.fullmatch(confirmation_id):
            return False
        try:
            removed = await client.eval(
                _REVOKE_SCRIPT,
                1,
                self._key(namespace, confirmation_id),
                thread_id or "",
                user_id or "",
            )
            return int(removed or 0) == 1
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return False


redis_confirmation_store = RedisConfirmationStore(
    redis_runtime,
    key_prefix=settings.REDIS_KEY_PREFIX,
    max_payload_bytes=settings.REDIS_CONFIRMATION_MAX_BYTES,
)

__all__ = ["RedisConfirmationStore", "redis_confirmation_store"]
