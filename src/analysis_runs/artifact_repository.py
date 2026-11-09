"""MongoDB persistence for bounded analysis artifacts."""

from __future__ import annotations

import asyncio
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel

from analysis_runs.artifact_contracts import AnalysisArtifactRecord
from core.settings import settings
from scat.infrastructure.database import flight_database


class AnalysisArtifactTooLargeError(ValueError):
    """Raised before MongoDB insertion when a payload exceeds the service boundary."""


class AnalysisArtifactRepository:
    def __init__(self, *, collection: Any | None = None) -> None:
        self._injected_collection = collection
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    @property
    def collection(self) -> Any:
        if self._injected_collection is not None:
            return self._injected_collection
        return flight_database.get_collection(settings.ANALYSIS_ARTIFACTS_COLLECTION)

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            if self._injected_collection is None:
                await flight_database.connect()
            await self.collection.create_indexes(
                [
                    IndexModel(
                        [("artifact_id", ASCENDING)],
                        unique=True,
                        name="uq_analysis_artifact_id",
                    ),
                    IndexModel(
                        [("run_id", ASCENDING), ("kind", ASCENDING)],
                        name="ix_analysis_artifact_run_kind",
                    ),
                    IndexModel(
                        [("owner_user_id", ASCENDING), ("created_at", DESCENDING)],
                        name="ix_analysis_artifact_owner_created",
                    ),
                    IndexModel(
                        [("expires_at", ASCENDING)],
                        expireAfterSeconds=0,
                        name="ttl_analysis_artifact_expiry",
                    ),
                ]
            )
            self._indexes_ready = True

    async def require_multipage(self) -> None:
        """Refuse an incompatible legacy collection before any GPU effect.

        Existing unique run/kind indexes are never silently removed: old deployments
        may still depend on them. Select a new collection or perform a reviewed migration.
        """
        if self._injected_collection is None:
            await flight_database.connect()
        indexes = await self.collection.index_information()
        if any(
            item.get("unique") and list(item.get("key", [])) == [("run_id", 1), ("kind", 1)]
            for item in indexes.values()
        ):
            raise ValueError(
                "GPU reports require a multipage artifact collection; legacy unique run/kind index remains"
            )
        await self.ensure_indexes()

    async def create(self, record: AnalysisArtifactRecord) -> None:
        if record.payload_size_bytes > settings.ANALYSIS_ARTIFACT_MAX_BYTES:
            raise AnalysisArtifactTooLargeError(
                "analysis artifact exceeds the configured payload size limit"
            )
        await self.ensure_indexes()
        await self.collection.insert_one(record.to_mongo_document())

    async def get_for_owner(
        self,
        artifact_id: str,
        *,
        owner_user_id: str,
    ) -> AnalysisArtifactRecord | None:
        await self.ensure_indexes()
        document = await self.collection.find_one(
            {"artifact_id": artifact_id, "owner_user_id": owner_user_id}
        )
        if document is None:
            return None
        return AnalysisArtifactRecord.from_mongo_document(document)

    async def list_for_owner(
        self,
        *,
        owner_user_id: str,
        limit: int = 20,
        model_key: str | None = None,
        operation: str | None = None,
        batch: str | None = None,
    ) -> list[AnalysisArtifactRecord]:
        """List recent artifact metadata inside one stable owner scope."""

        await self.ensure_indexes()
        filters: dict[str, Any] = {"owner_user_id": owner_user_id}
        if model_key is not None:
            filters["model_key"] = model_key
        if operation is not None:
            filters["analysis_result.operation"] = operation
        if batch is not None:
            filters["analysis_result.context.batch"] = batch
        safe_limit = min(max(int(limit), 1), 50)
        cursor = self.collection.find(filters).sort("created_at", DESCENDING).limit(safe_limit)
        documents = await cursor.to_list(length=safe_limit)
        return [AnalysisArtifactRecord.from_mongo_document(document) for document in documents]


analysis_artifact_repository = AnalysisArtifactRepository()


__all__ = [
    "AnalysisArtifactRepository",
    "AnalysisArtifactTooLargeError",
    "analysis_artifact_repository",
]
