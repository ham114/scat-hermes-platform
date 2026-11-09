"""Version-aware caches for immutable materialized read models."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Hashable
from copy import deepcopy
from time import monotonic
from typing import Any

from bson import json_util
from bson.json_util import JSONOptions

from core.redis_runtime import RedisRuntime, redis_runtime
from core.settings import settings

logger = logging.getLogger(__name__)

_SAFE_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JSON_OPTIONS = JSONOptions(tz_aware=True)


class VersionedMaterializationCache:
    """Bounded TTL cache whose caller-provided key must include data/model versions."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        max_entries: int = 512,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, key: Hashable) -> dict[str, Any] | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return deepcopy(value)

    def put(self, key: Hashable, value: dict[str, Any]) -> None:
        if self.ttl_seconds == 0:
            return
        self._items[key] = (self._clock() + self.ttl_seconds, deepcopy(value))
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def discard(self, key: Hashable) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()


class RedisMaterializationCache:
    """Optional shared positive cache; MongoDB remains authoritative."""

    def __init__(
        self,
        runtime: RedisRuntime,
        *,
        key_prefix: str,
        ttl_seconds: float,
        max_payload_bytes: int,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self._runtime = runtime
        self._key_prefix = key_prefix
        self._ttl_seconds = float(ttl_seconds)
        self._max_payload_bytes = int(max_payload_bytes)

    @property
    def enabled(self) -> bool:
        return self._runtime.enabled and self._ttl_seconds > 0

    def key(
        self,
        kind: str,
        *,
        business_key: dict[str, Any],
        source_version: str,
        algorithm_version: str,
    ) -> str:
        """Build a bounded key from every isolation dimension."""
        if not _SAFE_KIND.fullmatch(kind):
            raise ValueError("unsafe materialization kind")
        if not business_key or not source_version or not algorithm_version:
            raise ValueError("materialization cache keys require business and version fields")
        identity = json.dumps(
            {
                "business_key": business_key,
                "source_version": source_version,
                "algorithm_version": algorithm_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{self._key_prefix}:materialization:{kind}:{digest}"

    async def get(
        self,
        kind: str,
        *,
        business_key: dict[str, Any],
        source_version: str,
        algorithm_version: str,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        client = await self._runtime.get_client()
        if client is None:
            return None
        try:
            raw = await client.get(
                self.key(
                    kind,
                    business_key=business_key,
                    source_version=source_version,
                    algorithm_version=algorithm_version,
                )
            )
            if not isinstance(raw, str):
                return None
            envelope = json_util.loads(raw, json_options=_JSON_OPTIONS)
            if not isinstance(envelope, dict) or envelope.get("schema_version") != 1:
                return None
            document = envelope.get("document")
            if not isinstance(document, dict):
                return None
            if not _matches_identity(
                document,
                business_key=business_key,
                source_version=source_version,
                algorithm_version=algorithm_version,
            ):
                return None
            return document
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return None

    async def put(
        self,
        kind: str,
        document: dict[str, Any],
        *,
        business_key: dict[str, Any],
        source_version: str,
        algorithm_version: str,
    ) -> bool:
        """Cache only complete positive records that match the requested identity."""
        if not self.enabled or not _matches_identity(
            document,
            business_key=business_key,
            source_version=source_version,
            algorithm_version=algorithm_version,
        ):
            return False
        encoded = json_util.dumps(
            {"schema_version": 1, "document": document},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            json_options=_JSON_OPTIONS,
        )
        if len(encoded.encode("utf-8")) > self._max_payload_bytes:
            logger.warning("Redis materialization payload exceeded configured byte limit")
            return False
        client = await self._runtime.get_client()
        if client is None:
            return False
        try:
            result = await client.set(
                self.key(
                    kind,
                    business_key=business_key,
                    source_version=source_version,
                    algorithm_version=algorithm_version,
                ),
                encoded,
                ex=max(1, math.ceil(self._ttl_seconds)),
            )
            return bool(result)
        except Exception as error:  # noqa: BLE001 - optional dependency boundary
            await self._runtime.mark_failed(error)
            return False


def _matches_identity(
    document: dict[str, Any],
    *,
    business_key: dict[str, Any],
    source_version: str,
    algorithm_version: str,
) -> bool:
    if document.get("source_version") != source_version:
        return False
    if document.get("algorithm_version") != algorithm_version:
        return False
    return all(document.get(field) == value for field, value in business_key.items())


redis_materialization_cache = RedisMaterializationCache(
    redis_runtime,
    key_prefix=settings.REDIS_KEY_PREFIX,
    ttl_seconds=settings.SCAT_MATERIALIZATION_CACHE_TTL_SECONDS,
    max_payload_bytes=settings.REDIS_MATERIALIZATION_MAX_BYTES,
)


__all__ = [
    "RedisMaterializationCache",
    "VersionedMaterializationCache",
    "redis_materialization_cache",
]
