"""M6-06 实验记录集合读写：`scat_model_experiments`。

约束与物化集合一致：只写派生实验集合，不动原始集合；按 `experiment_id` 幂等 upsert；
索引由 `ensure_indexes` 显式创建（幂等）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

from core.identity_scope import current_owner, owner_filter
from core.redis_task_notifications import RedisTaskNotifier, redis_task_notifier
from scat.experiments.contracts import ExperimentRecord

EXPERIMENT_COLLECTION = "scat_model_experiments"

__all__ = ["EXPERIMENT_COLLECTION", "ExperimentRepository"]


class ExperimentRepository:
    """实验记录仓储：写入、读取、列表与索引。"""

    def __init__(
        self,
        mongo_database: Any,
        *,
        task_notifier: RedisTaskNotifier | None = None,
    ) -> None:
        self._database = mongo_database
        self._task_notifier = task_notifier or redis_task_notifier

    async def _notify(self, document: dict[str, Any] | None) -> None:
        if document is None:
            return
        experiment_id = document.get("experiment_id")
        status = document.get("status")
        if isinstance(experiment_id, str) and isinstance(status, str):
            await self._task_notifier.publish(
                domain="experiment",
                task_id=experiment_id,
                status=status,
            )

    def collection(self) -> Any:
        return self._database[EXPERIMENT_COLLECTION]

    @staticmethod
    def _eligible_claim_filter(*, now: datetime, max_attempts: int) -> dict[str, Any]:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        return {
            "status": "pending",
            "$and": [
                {
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": now}},
                    ]
                },
                {
                    "$expr": {
                        "$lt": [
                            {"$ifNull": ["$attempt_count", 0]},
                            {"$ifNull": ["$max_attempts", max_attempts]},
                        ]
                    }
                },
            ],
        }

    async def upsert(self, record: ExperimentRecord) -> None:
        """按 experiment_id 幂等写入/更新整条记录。"""
        if current_owner() is not None:
            record = record.model_copy(update={"owner_user_id": current_owner()})
        document = record.model_dump(mode="json")
        await self.collection().replace_one(
            {"experiment_id": record.experiment_id, **owner_filter()},
            document,
            upsert=True,
        )
        await self._notify(document)

    async def get(self, experiment_id: str) -> dict[str, Any] | None:
        document = await self.collection().find_one(
            {"experiment_id": experiment_id, **owner_filter()}
        )
        return document

    @staticmethod
    def _lease_fields(
        *,
        lease_owner: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not lease_owner.strip():
            raise ValueError("lease_owner 不能为空")
        if lease_ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl 必须大于 0")
        acquired_at = now if now is not None else datetime.now(UTC)
        return {
            "status": "running",
            "lease_owner": lease_owner,
            "lease_token": uuid4().hex,
            "lease_acquired_at": acquired_at,
            "lease_heartbeat_at": acquired_at,
            "lease_expires_at": acquired_at + lease_ttl,
        }

    async def claim(
        self,
        experiment_id: str,
        *,
        lease_owner: str = "unknown-worker",
        lease_ttl: timedelta = timedelta(minutes=5),
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """领取指定待执行实验（pending → running）；非 pending 或不存在时返回 None。"""
        claimed_at = now if now is not None else datetime.now(UTC)
        filters = self._eligible_claim_filter(now=claimed_at, max_attempts=max_attempts)
        filters["experiment_id"] = experiment_id
        document = await self.collection().find_one_and_update(
            filters,
            {
                "$set": {
                    **self._lease_fields(
                        lease_owner=lease_owner,
                        lease_ttl=lease_ttl,
                        now=claimed_at,
                    ),
                    "next_attempt_at": None,
                },
                "$inc": {"attempt_count": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        await self._notify(document)
        return document

    async def claim_next(
        self,
        *,
        lease_owner: str = "unknown-worker",
        lease_ttl: timedelta = timedelta(minutes=5),
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """按创建时间先序领取最早一条待执行实验（pending → running）。"""
        claimed_at = now if now is not None else datetime.now(UTC)
        document = await self.collection().find_one_and_update(
            self._eligible_claim_filter(now=claimed_at, max_attempts=max_attempts),
            {
                "$set": {
                    **self._lease_fields(
                        lease_owner=lease_owner,
                        lease_ttl=lease_ttl,
                        now=claimed_at,
                    ),
                    "next_attempt_at": None,
                },
                "$inc": {"attempt_count": 1},
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        await self._notify(document)
        return document

    async def heartbeat(
        self,
        experiment_id: str,
        *,
        lease_token: str,
        lease_ttl: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> bool:
        """仅由当前 lease token 续租；租约已被替换或任务不在 running 时返回 False。"""
        if not lease_token:
            return False
        if lease_ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl 必须大于 0")
        heartbeat_at = now if now is not None else datetime.now(UTC)
        document = await self.collection().find_one_and_update(
            {
                "experiment_id": experiment_id,
                "status": "running",
                "lease_token": lease_token,
            },
            {
                "$set": {
                    "lease_heartbeat_at": heartbeat_at,
                    "lease_expires_at": heartbeat_at + lease_ttl,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return document is not None

    async def finish(self, record: ExperimentRecord, *, lease_token: str) -> bool:
        """以 lease token 作为 fencing 条件提交终态；旧执行器无法覆盖新租约。"""
        if not lease_token:
            return False
        document = record.model_dump(mode="json")
        result = await self.collection().replace_one(
            {
                "experiment_id": record.experiment_id,
                "status": "running",
                "lease_token": lease_token,
            },
            document,
            upsert=False,
        )
        finished = bool(result.matched_count == 1)
        if finished:
            await self._notify(document)
        return finished

    async def requeue(
        self,
        experiment_id: str,
        *,
        reason: str,
        operator: str,
        retry_base_seconds: float = 60.0,
        retry_max_seconds: float = 3600.0,
        default_max_attempts: int = 3,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """人工重入过期 running/failed 实验；带退避、次数上限与有界审计。"""
        normalized_reason = reason.strip()
        normalized_operator = operator.strip()
        if not normalized_reason:
            raise ValueError("requeue reason 不能为空")
        if not normalized_operator:
            raise ValueError("requeue operator 不能为空")
        if len(normalized_reason) > 500 or len(normalized_operator) > 200:
            raise ValueError("requeue reason/operator 超出长度上限")
        if retry_base_seconds < 0 or retry_max_seconds < 0:
            raise ValueError("重试退避时间不能小于 0")
        if default_max_attempts < 1:
            raise ValueError("default_max_attempts 必须大于 0")

        requested_at = now if now is not None else datetime.now(UTC)
        current = await self.get(experiment_id)
        if current is None:
            return {"outcome": "not_found", "document": None}
        previous_status = str(current.get("status") or "")
        if previous_status not in {"running", "failed"}:
            return {"outcome": "invalid_status", "document": current}

        raw_lease_expires_at = current.get("lease_expires_at")
        lease_expires_at = raw_lease_expires_at
        if isinstance(lease_expires_at, str):
            lease_expires_at = datetime.fromisoformat(lease_expires_at)
        if isinstance(lease_expires_at, datetime) and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if previous_status == "running" and (
            not isinstance(lease_expires_at, datetime) or lease_expires_at > requested_at
        ):
            if lease_expires_at is not None:
                return {"outcome": "active_lease", "document": current}
            # Legacy running records predate leases and are explicitly recoverable.

        attempt_count = max(0, int(current.get("attempt_count") or 0))
        max_attempts = max(1, int(current.get("max_attempts") or default_max_attempts))
        base_delay = min(float(retry_base_seconds), float(retry_max_seconds))
        delay_seconds = min(base_delay * (2 ** max(attempt_count - 1, 0)), retry_max_seconds)
        next_attempt_at = requested_at + timedelta(seconds=delay_seconds)
        audit_event = {
            "requested_at": requested_at,
            "operator": normalized_operator,
            "reason": normalized_reason,
            "previous_status": previous_status,
            "previous_lease_owner": current.get("lease_owner"),
            "previous_lease_heartbeat_at": current.get("lease_heartbeat_at"),
            "previous_lease_expires_at": current.get("lease_expires_at"),
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
        }
        compare_filter: dict[str, Any] = {
            "experiment_id": experiment_id,
            "status": previous_status,
            "attempt_count": attempt_count,
        }
        if "attempt_count" not in current:
            compare_filter.pop("attempt_count")
            compare_filter["$or"] = [
                {"attempt_count": 0},
                {"attempt_count": {"$exists": False}},
            ]
        if previous_status == "running":
            compare_filter["lease_token"] = current.get("lease_token")
            compare_filter["lease_expires_at"] = (
                {"$lte": requested_at}
                if isinstance(lease_expires_at, datetime)
                else raw_lease_expires_at
            )

        if attempt_count >= max_attempts:
            audit_event["outcome"] = "retry_exhausted"
            document = await self.collection().find_one_and_update(
                compare_filter,
                {
                    "$set": {
                        "status": "failed",
                        "finished_at": requested_at,
                        "message": f"重试次数已耗尽（{attempt_count}/{max_attempts}）。",
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_acquired_at": None,
                        "lease_heartbeat_at": None,
                        "lease_expires_at": None,
                        "next_attempt_at": None,
                    },
                    "$inc": {"requeue_count": 1},
                    "$push": {"requeue_history": {"$each": [audit_event], "$slice": -20}},
                },
                return_document=ReturnDocument.AFTER,
            )
            await self._notify(document)
            return {
                "outcome": "retry_exhausted" if document is not None else "conflict",
                "document": document,
            }

        audit_event["outcome"] = "requeued"
        audit_event["next_attempt_at"] = next_attempt_at
        document = await self.collection().find_one_and_update(
            compare_filter,
            {
                "$set": {
                    "status": "pending",
                    "finished_at": None,
                    "message": f"由 {normalized_operator} 人工重新入队：{normalized_reason}",
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_acquired_at": None,
                    "lease_heartbeat_at": None,
                    "lease_expires_at": None,
                    "next_attempt_at": next_attempt_at,
                },
                "$inc": {"requeue_count": 1},
                "$push": {"requeue_history": {"$each": [audit_event], "$slice": -20}},
            },
            return_document=ReturnDocument.AFTER,
        )
        await self._notify(document)
        return {"outcome": "requeued" if document is not None else "conflict", "document": document}

    async def list_recent(
        self,
        *,
        batch: str | None = None,
        predictor_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按创建时间倒序列出实验摘要（有界字段）。"""
        safe_limit = max(1, min(int(limit), 100))
        filters: dict[str, Any] = owner_filter()
        if batch:
            filters["batch"] = batch
        if predictor_id:
            filters["predictor_id"] = predictor_id
        cursor = (
            self.collection()
            .find(
                filters,
                {
                    "_id": 0,
                    "experiment_id": 1,
                    "kind": 1,
                    "status": 1,
                    "batch": 1,
                    "predictor_id": 1,
                    "predictor_version": 1,
                    "data_version": 1,
                    "source": 1,
                    "requested_by": 1,
                    "created_at": 1,
                    "finished_at": 1,
                    "attempt_count": 1,
                    "max_attempts": 1,
                    "next_attempt_at": 1,
                    "requeue_count": 1,
                },
            )
            .sort("created_at", -1)
            .limit(safe_limit)
        )
        return list(await cursor.to_list(length=safe_limit))

    async def list_with_metrics(
        self,
        *,
        batch: str | None = None,
        predictor_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按创建时间倒序返回实验完整记录（含六档指标，不含 worst_samples），评估页对比用。"""
        safe_limit = max(1, min(int(limit), 50))
        filters: dict[str, Any] = owner_filter()
        if batch:
            filters["batch"] = batch
        if predictor_id:
            filters["predictor_id"] = predictor_id
        cursor = (
            self.collection()
            .find(filters, {"_id": 0, "worst_samples": 0})
            .sort("created_at", -1)
            .limit(safe_limit)
        )
        return list(await cursor.to_list(length=safe_limit))

    async def ensure_indexes(self) -> list[str]:
        """创建实验集合索引（幂等）：唯一 experiment_id + 批次/预测器查询索引。"""
        collection = self.collection()
        created = [
            await collection.create_index(
                [("experiment_id", 1)],
                unique=True,
                name="uq_model_experiments_id",
            ),
            await collection.create_index(
                [("batch", 1), ("predictor_id", 1), ("created_at", -1)],
                name="idx_model_experiments_batch_predictor",
            ),
            await collection.create_index(
                [("status", 1), ("lease_expires_at", 1)],
                name="idx_model_experiments_status_lease_expiry",
            ),
        ]
        return created
