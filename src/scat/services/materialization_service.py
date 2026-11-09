"""M4-09 物化结果读取：有效物化结果优先，缺失时明确回退（不静默重算）。

读取规则（与 M4 契约一致）：

- 只承认「当前 `source_version` + `algorithm_version`」且未过期的记录；版本不匹配或
  过期一律视为无有效结果，由调用方决定实时回退或提示物化任务；
- 读取路径不写派生集合：物化写入只由 `scat.materialization.jobs` / CLI 负责；
- 任务查询直接读 `scat_materialization_jobs`，列表默认按 `generated_at` 倒序并有界限定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from core.settings import settings
from scat.infrastructure.database import flight_database
from scat.materialization.cache import (
    RedisMaterializationCache,
    VersionedMaterializationCache,
    redis_materialization_cache,
)
from scat.materialization.payloads import data_quality_payload, statistics_payload
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    BATCH_STATISTICS_ALGORITHM_VERSION,
    DEFAULT_SOURCE_VERSION,
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    PREDICTION_METRICS_ALGORITHM_VERSION,
)
from scat.services.flight_service import ScatFlightService

JOB_LIST_LIMIT_MAX = 200
JOB_LIST_LIMIT_DEFAULT = 50

# 任务种类白名单：与物化派生集合（不含 jobs 自身）一致，避免任意过滤字段。
JOB_KINDS: tuple[str, ...] = (
    "batch_statistics",
    "route_statistics",
    "flight_features",
    "phase_segments",
    "prediction_metrics",
)
JOB_STATUSES: tuple[str, ...] = ("pending", "running", "succeeded", "partial", "failed")

StatisticsSource = Literal["auto", "live"]


class MaterializationService:
    """派生集合读取入口（批次统计、航班特征、物化任务状态）。"""

    def __init__(
        self,
        repository: MaterializationRepository | None = None,
        *,
        flight_service: ScatFlightService | None = None,
        cache: VersionedMaterializationCache | None = None,
        shared_cache: RedisMaterializationCache | None = None,
    ) -> None:
        self._repository = repository
        self.flight_service = flight_service or ScatFlightService()
        self._cache = cache or VersionedMaterializationCache(
            ttl_seconds=settings.SCAT_MATERIALIZATION_CACHE_TTL_SECONDS,
            max_entries=settings.SCAT_MATERIALIZATION_CACHE_MAX_ENTRIES,
        )
        self._shared_cache = shared_cache or redis_materialization_cache

    @property
    def repository(self) -> MaterializationRepository:
        if self._repository is None:
            self._repository = MaterializationRepository(flight_database.db)
        return self._repository

    async def read_batch_statistics(
        self,
        *,
        batch: str,
        statistics_source: StatisticsSource = "auto",
        source_version: str = DEFAULT_SOURCE_VERSION,
        algorithm_version: str = BATCH_STATISTICS_ALGORITHM_VERSION,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """批次统计读取：`auto` 优先有效物化结果，否则实时计算并说明回退原因。"""
        query = {"batch": batch}
        reason: str | None = None
        if statistics_source == "auto":
            document = await self._find_cached_latest(
                "batch_statistics",
                batch=batch,
                source_version=source_version,
                algorithm_version=algorithm_version,
            )
            if document is not None and not _expired(document, now):
                return {
                    "status": "ok",
                    "query": query,
                    "data": {
                        "batch": batch,
                        "source": "materialized",
                        "statistics": statistics_payload(document),
                        "data_quality": data_quality_payload(document),
                        "generated_at": document.get("generated_at"),
                        "source_version": document.get("source_version"),
                        "algorithm_version": document.get("algorithm_version"),
                        "fallback_reason": None,
                    },
                }
            reason = "expired" if document is not None else "missing"

        statistics = await self.flight_service.repository.get_batch_statistics(batch=batch)
        if statistics is None:
            return {
                "status": "not_found",
                "message": f"没有找到批次 {batch}",
                "query": query,
                "data": None,
            }
        quality = await self.flight_service.repository.get_batch_data_quality(batch=batch)
        return {
            "status": "ok",
            "query": query,
            "data": {
                "batch": batch,
                "source": "live",
                "statistics": statistics,
                "data_quality": quality,
                "generated_at": None,
                "source_version": None,
                "algorithm_version": None,
                "fallback_reason": reason,
            },
        }

    async def get_batch_statistics(
        self,
        *,
        batch: str,
        source_version: str = DEFAULT_SOURCE_VERSION,
        algorithm_version: str = BATCH_STATISTICS_ALGORITHM_VERSION,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """仅返回有效物化记录（供总览等读路径复用）；无有效结果返回 None。"""
        document = await self._find_cached_latest(
            "batch_statistics",
            batch=batch,
            source_version=source_version,
            algorithm_version=algorithm_version,
        )
        if document is None or _expired(document, now):
            return None
        return document

    async def get_flight_features(
        self,
        *,
        batch: str,
        external_id: str | int,
        source_version: str = DEFAULT_SOURCE_VERSION,
        algorithm_version: str = FLIGHT_FEATURES_ALGORITHM_VERSION,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        document = await self._find_cached_latest(
            "flight_features",
            batch=batch,
            external_id=str(external_id),
            source_version=source_version,
            algorithm_version=algorithm_version,
        )
        if document is None or _expired(document, now):
            return None
        return document

    async def get_flight_phase_segments(
        self,
        *,
        batch: str,
        external_id: str | int,
        source_version: str = DEFAULT_SOURCE_VERSION,
        algorithm_version: str = FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """读取单航班的段级阶段标注（M4-12）；缺失或过期返回 None。"""
        document = await self._find_cached_latest(
            "phase_segments",
            batch=batch,
            external_id=str(external_id),
            source_version=source_version,
            algorithm_version=algorithm_version,
        )
        if document is None or _expired(document, now):
            return None
        return document

    async def _find_cached_latest(
        self,
        kind: str,
        *,
        batch: str,
        source_version: str,
        algorithm_version: str,
        external_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read a positive cache keyed by the complete materialization version."""
        business_key: dict[str, Any] = {
            "batch": batch,
        }
        if external_id is not None:
            business_key["external_id"] = external_id
        versioned_query = {
            **business_key,
            "source_version": source_version,
            "algorithm_version": algorithm_version,
        }
        if self._shared_cache.enabled:
            cached = await self._shared_cache.get(
                kind,
                business_key=business_key,
                source_version=source_version,
                algorithm_version=algorithm_version,
            )
        else:
            key = (kind, batch, external_id, source_version, algorithm_version)
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        document = await self.repository.find_latest(kind, versioned_query)
        if document is not None and not _expired(document, None):
            if self._shared_cache.enabled:
                await self._shared_cache.put(
                    kind,
                    document,
                    business_key=business_key,
                    source_version=source_version,
                    algorithm_version=algorithm_version,
                )
            elif (
                document.get("source_version") == source_version
                and document.get("algorithm_version") == algorithm_version
                and all(document.get(field) == value for field, value in business_key.items())
            ):
                self._cache.put(key, document)
        return document

    async def list_metric_summaries(
        self,
        *,
        batch: str,
        group_by: str | None = None,
        horizon_s: float | None = None,
        source_version: str = DEFAULT_SOURCE_VERSION,
        algorithm_version: str = PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """M5-08 评估页数据：读取指标汇总（按分组维度/时域筛选，只读、有界）。

        返回 overall / phase / aircraft_type 的 MAE/RMSE/样本量与 overall 的分位数，
        供模型评估页展示与导出（导出由客户端把同一份 JSON 转 CSV）。
        """
        safe_limit = max(1, min(int(limit), 500))
        filters: dict[str, Any] = {
            "batch": batch,
            "source_version": source_version,
            "algorithm_version": algorithm_version,
        }
        if group_by is not None:
            filters["group_by"] = group_by
        if horizon_s is not None:
            filters["horizon_s"] = float(horizon_s)
        cursor = (
            self.repository.collection("prediction_metric_summaries")
            .find(filters, {"_id": 0})
            .sort([("group_by", 1), ("horizon_s", 1), ("sample_count", -1), ("group_value", 1)])
            .limit(safe_limit)
        )
        if offset:
            cursor = cursor.skip(max(0, int(offset)))
        rows = await cursor.to_list(length=safe_limit)
        return {
            "batch": batch,
            "group_by": group_by,
            "horizon_s": horizon_s,
            "record_count": len(rows),
            "summaries": rows,
        }

    async def list_worst_prediction_samples(
        self,
        *,
        batch: str,
        metric: str = "horizontal_error_m",
        horizon_s: float | None = None,
        limit: int = 20,
        source_version: str = DEFAULT_SOURCE_VERSION,
        algorithm_version: str = PREDICTION_METRICS_ALGORITHM_VERSION,
        offset: int = 0,
    ) -> dict[str, Any]:
        """M5-07 最差案例检索（只读）：按样本级误差倒序取 Top-K，并附航班主导阶段与机型。

        走 `(batch, <metric>)` 索引（`ensure-indexes` 建）；只取有效样本；`limit` ≤ 100。
        """
        if metric not in {
            "horizontal_error_m",
            "vertical_error_m",
            "along_track_error_m",
            "cross_track_error_m",
        }:
            raise ValueError(f"未知误差字段: {metric}")
        safe_limit = max(1, min(int(limit), 100))
        filters: dict[str, Any] = {
            "batch": batch,
            "source_version": source_version,
            "algorithm_version": algorithm_version,
            "is_valid": True,
            metric: {"$ne": None},
        }
        if horizon_s is not None:
            filters["horizon_s"] = float(horizon_s)
        cursor = (
            self.repository.collection("prediction_metrics")
            .find(filters, {"_id": 0})
            .sort([(metric, -1), ("external_id", 1), ("snapshot_time", 1), ("horizon_s", 1)])
            .limit(safe_limit)
        )
        if offset:
            cursor = cursor.skip(max(0, int(offset)))
        rows = await cursor.to_list(length=safe_limit)
        external_ids = sorted({str(row.get("external_id")) for row in rows})
        attributes: dict[str, dict[str, Any]] = {}
        if external_ids:
            attributes = await self._flight_attributes(
                batch=batch,
                external_ids=external_ids,
                source_version=source_version,
            )
        samples = [
            {
                **row,
                "dominant_phase": (attributes.get(str(row.get("external_id"))) or {}).get(
                    "dominant_phase"
                ),
                "aircraft_type": (attributes.get(str(row.get("external_id"))) or {}).get(
                    "aircraft_type"
                ),
            }
            for row in rows
        ]
        return {
            "batch": batch,
            "metric": metric,
            "horizon_s": horizon_s,
            "sample_count": len(samples),
            "samples": samples,
        }

    async def _flight_attributes(
        self,
        *,
        batch: str,
        external_ids: list[str],
        source_version: str,
    ) -> dict[str, dict[str, Any]]:
        """批量读取航班的阶段与机型（M5-07 附属信息；有界，只读）。"""
        attributes: dict[str, dict[str, Any]] = {key: {} for key in external_ids}
        for kind, field, version in (
            ("phase_segments", "dominant_phase", FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION),
            ("flight_features", "aircraft_type", FLIGHT_FEATURES_ALGORITHM_VERSION),
        ):
            cursor = await self.repository.collection(kind).aggregate(
                [
                    {
                        "$match": {
                            "batch": batch,
                            "source_version": source_version,
                            "algorithm_version": version,
                            "external_id": {"$in": external_ids},
                        }
                    },
                    {"$project": {"_id": 0, "external_id": 1, field: 1}},
                ]
            )
            for document in await cursor.to_list(length=None):
                identifier = str(document.get("external_id"))
                attributes.setdefault(identifier, {})[field] = document.get(field)
        return attributes

    async def list_jobs(
        self,
        *,
        kind: str | None = None,
        batch: str | None = None,
        status_filter: str | None = None,
        limit: int = JOB_LIST_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """物化任务列表（有界）；用于查询进度、失败单位与错误原因。"""
        if limit < 1 or limit > JOB_LIST_LIMIT_MAX:
            raise ValueError(f"limit 必须在 1～{JOB_LIST_LIMIT_MAX} 之间")
        if kind is not None and kind not in JOB_KINDS:
            raise ValueError(f"未知物化任务种类: {kind}")
        if status_filter is not None and status_filter not in JOB_STATUSES:
            raise ValueError(f"未知任务状态: {status_filter}")
        from core.identity_scope import owner_filter

        filters: dict[str, Any] = owner_filter()
        if kind:
            filters["kind"] = kind
        if batch:
            filters["batch"] = batch
        if status_filter:
            filters["status"] = status_filter
        cursor = (
            self.repository.collection("jobs")
            .find(filters, {"failure_detail": 0})
            .sort("generated_at", -1)
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        items = [_job_payload(document) for document in documents]
        return {
            "status": "ok",
            "query": {"kind": kind, "batch": batch, "status": status_filter},
            "data": {"items": items, "limit": limit, "returned": len(items)},
        }

    async def get_job(self, job_id: str) -> dict[str, Any]:
        document = await self.repository.find_job(job_id)
        if document is None:
            return {
                "status": "not_found",
                "message": f"没有找到物化任务 {job_id}",
                "query": {"job_id": job_id},
                "data": None,
            }
        return {"status": "ok", "query": {"job_id": job_id}, "data": _job_payload(document)}


def _expired(document: dict[str, Any], now: datetime | None) -> bool:
    expires_at = document.get("expires_at")
    if expires_at is None:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or datetime.now(UTC))


def _job_payload(document: dict[str, Any]) -> dict[str, Any]:
    failed_targets = [str(value) for value in document.get("failed_targets") or []]
    return {
        "job_id": document.get("job_id"),
        "kind": document.get("kind"),
        "batch": document.get("batch"),
        "status": document.get("status"),
        "progress": float(document.get("progress") or 0.0),
        "total_count": int(document.get("total_count") or 0),
        "processed_count": int(document.get("processed_count") or 0),
        "failed_count": len(failed_targets),
        "failed_targets": failed_targets,
        "failure_detail": {
            str(key): str(value)[:300]
            for key, value in (document.get("failure_detail") or {}).items()
        },
        "error_message": document.get("error_message"),
        "retry_of": document.get("retry_of"),
        "generated_at": document.get("generated_at"),
        "expires_at": document.get("expires_at"),
    }


__all__ = [
    "JOB_KINDS",
    "JOB_LIST_LIMIT_DEFAULT",
    "JOB_LIST_LIMIT_MAX",
    "JOB_STATUSES",
    "MaterializationService",
]
