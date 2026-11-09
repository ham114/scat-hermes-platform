"""M4-10 派生集合维护：索引创建、旧版本清理与「从原始数据重建」支持。

原则：
- 只操作物化派生集合，绝不删除 `scat_flights` / `uav_flights` 等原始集合；
- 清理默认 dry-run（只统计），需要显式 apply 才删除；
- 「重建」= 删除当前版本结果后按同一口径重算，不改变 source_version/algorithm_version
  语义（版本递增仍由口径变更决定）。
"""

from __future__ import annotations

from typing import Any, Final

from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    BATCH_STATISTICS_ALGORITHM_VERSION,
    DEFAULT_SOURCE_VERSION,
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    PREDICTION_METRICS_ALGORITHM_VERSION,
)

# 已实现物化任务的种类 → 当前算法版本（清理时只保留当前版本，其余视为旧版本）。
MATERIALIZED_KINDS: Final[dict[str, str]] = {
    "batch_statistics": BATCH_STATISTICS_ALGORITHM_VERSION,
    "flight_features": FLIGHT_FEATURES_ALGORITHM_VERSION,
    "phase_segments": FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    "prediction_metrics": PREDICTION_METRICS_ALGORITHM_VERSION,
    "prediction_metric_summaries": PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
}


async def cleanup_stale_versions(
    repository: MaterializationRepository,
    *,
    source_version: str = DEFAULT_SOURCE_VERSION,
    kind: str | None = None,
    batch: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """删除与当前「源数据版本 + 算法版本」不一致的旧结果；dry_run 只统计。"""
    kinds = [kind] if kind else list(MATERIALIZED_KINDS)
    unknown = [item for item in kinds if item not in MATERIALIZED_KINDS]
    if unknown:
        raise ValueError(f"未知物化集合种类: {', '.join(unknown)}")

    filters_common: dict[str, Any] = {}
    if batch:
        filters_common["batch"] = batch

    results: list[dict[str, Any]] = []
    for current in kinds:
        algorithm_version = MATERIALIZED_KINDS[current]
        filters: dict[str, Any] = {
            **filters_common,
            "$or": [
                {"source_version": {"$ne": source_version}},
                {"algorithm_version": {"$ne": algorithm_version}},
            ],
        }
        collection = repository.collection(current)
        if dry_run:
            matched = await collection.count_documents(filters)
            deleted = 0
        else:
            outcome = await collection.delete_many(filters)
            matched = deleted = int(outcome.deleted_count)
        results.append(
            {
                "kind": current,
                "source_version": source_version,
                "algorithm_version": algorithm_version,
                "matched": matched,
                "deleted": deleted,
            }
        )

    return {
        "dry_run": dry_run,
        "source_version": source_version,
        "batch": batch,
        "results": results,
        "deleted_total": sum(item["deleted"] for item in results),
    }


async def purge_batch_version(
    repository: MaterializationRepository,
    *,
    kind: str,
    batch: str,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str | None = None,
) -> int:
    """删除某批次当前版本的派生结果，用于「删除派生结果后仅从原始数据重建」（M4-10）。"""
    if kind not in MATERIALIZED_KINDS:
        raise ValueError(f"未知物化集合种类: {kind}")
    resolved_version = algorithm_version or MATERIALIZED_KINDS[kind]
    outcome = await repository.collection(kind).delete_many(
        {
            "batch": batch,
            "source_version": source_version,
            "algorithm_version": resolved_version,
        }
    )
    return int(outcome.deleted_count)


__all__ = ["MATERIALIZED_KINDS", "cleanup_stale_versions", "purge_batch_version"]
