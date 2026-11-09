"""M4 物化集合读写仓库：幂等 upsert、读取、任务记录与索引（M4-04 起）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pymongo import ReplaceOne
from pymongo.errors import OperationFailure

from core.identity_scope import current_owner, owner_filter
from core.redis_task_notifications import RedisTaskNotifier, redis_task_notifier
from scat.materialization.contracts import (
    MATERIALIZATION_BUSINESS_KEYS,
    MATERIALIZATION_COLLECTIONS,
    MATERIALIZATION_INDEXES,
    MATERIALIZATION_TTL_INDEXES,
    MATERIALIZATION_VERSION_KEYS,
    MaterializationJobRecord,
    MaterializedRecord,
)

MaterializationKind = str

# MongoDB 索引冲突错误码：IndexOptionsConflict / IndexKeySpecsConflict。
_INDEX_CONFLICT_CODES = frozenset({85, 86})


class MaterializationRepository:
    """派生集合的读写入口。

    - upsert_record：按「业务键 + 版本键」过滤 replace_one(upsert=True)，
      同一任务重复执行结果一致、不产生重复文档（M4 幂等约束）；
      算法/数据版本变化时旧记录不被覆盖（M4 版本隔离约束）。
    - 只写派生集合，绝不接触原始集合（原始集合只读约束）。
    """

    def __init__(
        self,
        mongo_database: Any,
        *,
        task_notifier: RedisTaskNotifier | None = None,
    ) -> None:
        self._database = mongo_database
        self._task_notifier = task_notifier or redis_task_notifier

    def collection(self, kind: MaterializationKind) -> Any:
        name = MATERIALIZATION_COLLECTIONS.get(kind)
        if name is None:
            raise ValueError(f"未知物化集合种类: {kind}")
        return self._database[name]

    def unique_filter(
        self, kind: MaterializationKind, record: MaterializedRecord
    ) -> dict[str, Any]:
        business_keys = MATERIALIZATION_BUSINESS_KEYS.get(kind)
        if business_keys is None:
            raise ValueError(f"未知物化集合种类: {kind}")
        values = record.model_dump()
        filters = {
            key: values[key]
            for key in [*business_keys, *MATERIALIZATION_VERSION_KEYS]
            if key in values
        }
        if kind == "jobs":
            filters.update(owner_filter())
        return filters

    async def upsert_record(
        self,
        kind: MaterializationKind,
        record: MaterializedRecord,
    ) -> None:
        document = record.model_dump(mode="json")
        await self.collection(kind).replace_one(
            self.unique_filter(kind, record),
            document,
            upsert=True,
        )

    async def upsert_records(
        self,
        kind: MaterializationKind,
        records: Sequence[MaterializedRecord],
    ) -> int:
        """Write one bounded batch with unordered, idempotent replacements."""
        if not records:
            return 0
        operations = [
            ReplaceOne(
                self.unique_filter(kind, record),
                record.model_dump(mode="json"),
                upsert=True,
            )
            for record in records
        ]
        await self.collection(kind).bulk_write(operations, ordered=False)
        return len(operations)

    async def find_latest(
        self,
        kind: MaterializationKind,
        business_key: dict[str, Any],
    ) -> dict[str, Any] | None:
        """读取给定业务键下最新一条记录（generated_at 倒序；M4-09 读取策略复用）。"""
        document = await self.collection(kind).find_one(
            business_key,
            sort=[("generated_at", -1)],
        )
        return document

    async def ensure_indexes(self) -> list[str]:
        """按 M4-02 声明创建派生集合索引（幂等，可重复执行）。

        - 唯一键索引 = 业务键 + 版本键，保证幂等写入与版本隔离；
        - 查询索引来自 `MATERIALIZATION_INDEXES`；
        - 保留策略由 `MATERIALIZATION_TTL_INDEXES` 的 TTL 索引执行（jobs `expires_at`）。
        """
        created: list[str] = []
        for kind in MATERIALIZATION_COLLECTIONS:
            collection = self.collection(kind)
            unique_fields = [
                *MATERIALIZATION_BUSINESS_KEYS[kind],
                *MATERIALIZATION_VERSION_KEYS,
            ]
            created.append(
                await collection.create_index(
                    [(field, 1) for field in unique_fields],
                    unique=True,
                    name=f"uq_{kind}_versioned",
                )
            )
            for fields in MATERIALIZATION_INDEXES.get(kind, ()):
                created.append(
                    await collection.create_index(
                        [(field, 1) for field in fields],
                        name=f"idx_{kind}_{'_'.join(fields)}",
                    )
                )
        for kind, ttl_fields in MATERIALIZATION_TTL_INDEXES.items():
            for field in ttl_fields:
                created.append(await self._ensure_ttl_index(kind, field))
        return created

    async def _ensure_ttl_index(self, kind: MaterializationKind, field: str) -> str:
        """创建 TTL 索引；同一 key 上的历史普通索引先删除再重建（只作用于派生集合）。"""
        collection = self.collection(kind)
        name = f"ttl_{kind}_{field}"
        try:
            return await collection.create_index(
                [(field, 1)],
                expireAfterSeconds=0,
                name=name,
            )
        except OperationFailure as error:
            if error.code not in _INDEX_CONFLICT_CODES:
                raise
        information = await collection.index_information()
        for index_name, spec in information.items():
            if index_name == name:
                continue
            if [item[0] for item in spec.get("key") or ()] == [field]:
                await collection.drop_index(index_name)
        return await collection.create_index(
            [(field, 1)],
            expireAfterSeconds=0,
            name=name,
        )

    async def existing_values(
        self,
        kind: MaterializationKind,
        field: str,
        business_key: dict[str, Any],
    ) -> set[str]:
        """返回已物化记录在指定业务键字段上的取值集合。

        增量重算（M4-06）用它跳过已完成的单位；只取键字段，不加载整条文档，
        因此即使集合很大也保持有界内存。
        """
        values = await self.collection(kind).distinct(field, business_key)
        return {str(value) for value in values}

    async def upsert_job(self, job: MaterializationJobRecord) -> None:
        """写入/更新任务记录（同一 job_id 始终只有一条文档）。"""
        if current_owner() is not None:
            job = job.model_copy(update={"owner_user_id": current_owner()})
        await self.upsert_record("jobs", job)
        await self._task_notifier.publish(
            domain="materialization",
            task_id=job.job_id,
            status=job.status,
        )

    async def find_job(self, job_id: str) -> dict[str, Any] | None:
        """按 job_id 读取任务记录（重试与进度查询）。"""
        document = await self.collection("jobs").find_one(
            {"job_id": job_id, **owner_filter()},
            sort=[("generated_at", -1)],
        )
        return document

    async def count_records(
        self,
        kind: MaterializationKind,
        business_key: dict[str, Any],
    ) -> int:
        return int(await self.collection(kind).count_documents(business_key))


__all__ = ["MaterializationRepository"]
