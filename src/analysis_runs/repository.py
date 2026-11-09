"""Atomic MongoDB persistence for model-run lifecycle records."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel

from analysis_runs.contracts import (
    AnalysisRunError,
    AnalysisRunRecord,
    AnalysisRunStatusEvent,
)
from core.settings import settings
from scat.infrastructure.database import flight_database
from scat_model_runtime.contracts import ModelRunStatus


class AnalysisRunConflictError(RuntimeError):
    """Raised when an atomic status transition no longer matches stored state."""


class AnalysisRunRepository:
    """Store compact run metadata separately from future large artifacts."""

    def __init__(self, *, collection: Any | None = None) -> None:
        self._injected_collection = collection
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    @property
    def collection(self) -> Any:
        if self._injected_collection is not None:
            return self._injected_collection
        return flight_database.get_collection(settings.ANALYSIS_RUNS_COLLECTION)

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
                        [("run_id", ASCENDING)],
                        unique=True,
                        name="uq_analysis_run_id",
                    ),
                    IndexModel(
                        [("owner_user_id", ASCENDING), ("created_at", DESCENDING)],
                        name="ix_analysis_run_owner_created",
                    ),
                    IndexModel(
                        [("thread_id", ASCENDING), ("created_at", DESCENDING)],
                        name="ix_analysis_run_thread_created",
                    ),
                    IndexModel(
                        [("status", ASCENDING), ("updated_at", DESCENDING)],
                        name="ix_analysis_run_status_updated",
                    ),
                    IndexModel(
                        [("expires_at", ASCENDING)],
                        expireAfterSeconds=0,
                        name="ttl_analysis_run_expiry",
                    ),
                ]
            )
            self._indexes_ready = True

    async def create(self, record: AnalysisRunRecord) -> None:
        await self.ensure_indexes()
        await self.collection.insert_one(record.to_mongo_document())

    async def transition(
        self,
        run_id: str,
        *,
        expected_statuses: Iterable[ModelRunStatus],
        status: ModelRunStatus,
        progress: float,
        at: datetime | None = None,
        message: str | None = None,
        error: AnalysisRunError | None = None,
        result_summary: dict[str, Any] | None = None,
        result_schema_version: str | None = None,
        result_artifact_ids: tuple[str, ...] = (),
    ) -> None:
        """Advance a run once, guarded by its previous status."""

        await self.ensure_indexes()
        event_at = at or datetime.now(UTC)
        expected = tuple(dict.fromkeys(expected_statuses))
        if not expected:
            raise ValueError("expected_statuses cannot be empty")
        if progress < 0 or progress > 1:
            raise ValueError("progress must be between 0 and 1")
        if status == ModelRunStatus.FAILED and error is None:
            raise ValueError("failed transition requires an error")
        if status != ModelRunStatus.FAILED and error is not None:
            raise ValueError("only failed transition accepts an error")

        set_values: dict[str, Any] = {
            "status": status.value,
            "progress": progress,
            "updated_at": event_at,
        }
        if status == ModelRunStatus.RUNNING:
            set_values["started_at"] = event_at
        if status in {
            ModelRunStatus.SUCCEEDED,
            ModelRunStatus.PARTIAL,
            ModelRunStatus.FAILED,
            ModelRunStatus.CANCELLED,
        }:
            set_values["finished_at"] = event_at
        if error is not None:
            set_values["error"] = error.model_dump(mode="python")
        if result_summary is not None:
            set_values["result_summary"] = result_summary
        if result_schema_version is not None:
            set_values["result_schema_version"] = result_schema_version
        if result_artifact_ids:
            set_values["result_artifact_ids"] = list(result_artifact_ids)

        event = AnalysisRunStatusEvent(
            status=status,
            progress=progress,
            at=event_at,
            message=message,
        )
        result = await self.collection.update_one(
            {
                "run_id": run_id,
                "status": {"$in": [item.value for item in expected]},
            },
            {
                "$set": set_values,
                "$push": {"status_history": event.model_dump(mode="python", exclude_none=True)},
            },
        )
        if result.matched_count != 1:
            raise AnalysisRunConflictError(
                f"analysis run {run_id} was missing or no longer in an expected status"
            )

    async def get_for_owner(
        self,
        run_id: str,
        *,
        owner_user_id: str,
    ) -> AnalysisRunRecord | None:
        """Read one run only inside the caller's stable user scope."""

        await self.ensure_indexes()
        document = await self.collection.find_one(
            {"run_id": run_id, "owner_user_id": owner_user_id}
        )
        if document is None:
            return None
        return AnalysisRunRecord.from_mongo_document(document)

    async def list_for_owner(
        self,
        *,
        owner_user_id: str,
        limit: int = 20,
        model_key: str | None = None,
        status: ModelRunStatus | None = None,
    ) -> list[AnalysisRunRecord]:
        """List recent runs inside one stable owner scope."""

        await self.ensure_indexes()
        filters: dict[str, Any] = {"owner_user_id": owner_user_id}
        if model_key is not None:
            filters["model_key"] = model_key
        if status is not None:
            filters["status"] = status.value
        safe_limit = min(max(int(limit), 1), 50)
        cursor = self.collection.find(filters).sort("created_at", DESCENDING).limit(safe_limit)
        documents = await cursor.to_list(length=safe_limit)
        return [AnalysisRunRecord.from_mongo_document(document) for document in documents]


analysis_run_repository = AnalysisRunRepository()


__all__ = [
    "AnalysisRunConflictError",
    "AnalysisRunRepository",
    "analysis_run_repository",
]
