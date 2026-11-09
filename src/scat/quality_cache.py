"""Optional shared cache for compact SCAT collection-quality snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from bson import json_util
from bson.json_util import JSONOptions

from core.redis_runtime import RedisRuntime, redis_runtime
from core.settings import settings

logger = logging.getLogger(__name__)

QUALITY_SNAPSHOT_GENERATION_VERSION = "scat-quality-snapshot-v1"
_JSON_OPTIONS = JSONOptions(tz_aware=True)
_QUALITY_ROW_FIELDS = frozenset(
    {
        "batch",
        "external_id",
        "original_filename",
        "callsign",
        "trajectory_key",
        "point_count",
        "missing_point_count",
        "outlier_point_count",
        "spike_value_count",
    }
)


class RedisQualitySnapshotCache:
    """Caches only rebuildable aggregate summaries and compact per-flight rows."""

    def __init__(
        self,
        runtime: RedisRuntime,
        *,
        key_prefix: str,
        generation_version: str,
        ttl_seconds: float,
        max_payload_bytes: int,
    ) -> None:
        if not generation_version:
            raise ValueError("quality snapshot generation version is required")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self._runtime = runtime
        self._key_prefix = key_prefix
        self.generation_version = generation_version
        self.ttl_seconds = float(ttl_seconds)
        self._max_payload_bytes = int(max_payload_bytes)

    @property
    def enabled(self) -> bool:
        return self._runtime.enabled and self.ttl_seconds > 0

    def key(self, *, batch: str | None, policy_version: str) -> str:
        if not policy_version:
            raise ValueError("quality policy version is required")
        identity = json.dumps(
            {
                "batch": batch,
                "generation_version": self.generation_version,
                "policy_version": policy_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{self._key_prefix}:quality_snapshot:{digest}"

    async def get(self, *, batch: str | None, policy_version: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        client = await self._runtime.get_client()
        if client is None:
            return None
        try:
            raw = await client.get(self.key(batch=batch, policy_version=policy_version))
            if not isinstance(raw, str):
                return None
            envelope = json_util.loads(raw, json_options=_JSON_OPTIONS)
            if not isinstance(envelope, dict):
                return None
            if envelope.get("schema_version") != 1:
                return None
            if envelope.get("generation_version") != self.generation_version:
                return None
            if envelope.get("policy_version") != policy_version:
                return None
            if envelope.get("batch") != batch:
                return None
            snapshot = envelope.get("snapshot")
            if not _valid_snapshot(snapshot, batch=batch, policy_version=policy_version):
                return None
            return snapshot
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return None

    async def put(
        self,
        snapshot: dict[str, Any],
        *,
        batch: str | None,
        policy_version: str,
    ) -> bool:
        if not self.enabled or not _valid_snapshot(
            snapshot,
            batch=batch,
            policy_version=policy_version,
        ):
            return False
        encoded = json_util.dumps(
            {
                "schema_version": 1,
                "generation_version": self.generation_version,
                "policy_version": policy_version,
                "batch": batch,
                "generated_at": datetime.now(UTC),
                "snapshot": snapshot,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            json_options=_JSON_OPTIONS,
        )
        if len(encoded.encode("utf-8")) > self._max_payload_bytes:
            logger.warning("Redis quality snapshot exceeded configured byte limit")
            return False
        client = await self._runtime.get_client()
        if client is None:
            return False
        try:
            stored = await client.set(
                self.key(batch=batch, policy_version=policy_version),
                encoded,
                ex=max(1, math.ceil(self.ttl_seconds)),
            )
            return bool(stored)
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return False

    async def delete(self, *, batch: str | None, policy_version: str) -> bool:
        if not self.enabled:
            return False
        client = await self._runtime.get_client()
        if client is None:
            return False
        try:
            return bool(
                await client.delete(self.key(batch=batch, policy_version=policy_version))
            )
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return False


def _valid_snapshot(
    value: Any,
    *,
    batch: str | None,
    policy_version: str,
) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("quality_policy_version") != policy_version:
        return False
    expected_scope = "all_scat_flights" if batch is None else "batch"
    if value.get("scope") != expected_scope or value.get("batch") != batch:
        return False
    rows = value.get("_quality_flight_rows")
    if not isinstance(rows, list):
        return False
    return all(
        isinstance(row, dict)
        and set(row) <= _QUALITY_ROW_FIELDS
        and {"batch", "external_id", "point_count"} <= set(row)
        for row in rows
    )


redis_quality_snapshot_cache = RedisQualitySnapshotCache(
    redis_runtime,
    key_prefix=settings.REDIS_KEY_PREFIX,
    generation_version=QUALITY_SNAPSHOT_GENERATION_VERSION,
    ttl_seconds=settings.SCAT_QUALITY_SNAPSHOT_TTL_SECONDS,
    max_payload_bytes=settings.REDIS_QUALITY_SNAPSHOT_MAX_BYTES,
)


__all__ = [
    "QUALITY_SNAPSHOT_GENERATION_VERSION",
    "RedisQualitySnapshotCache",
    "redis_quality_snapshot_cache",
]
