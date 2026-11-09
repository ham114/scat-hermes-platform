"""M4 物化任务实现（M4-04 批次统计；M4-05 航班特征）。

任务约束（计划 M4）：
- 幂等：重复执行 produce 相同文档（upsert 语义由 MaterializationRepository 保证）；
- 不修改原始集合：只读聚合、只写派生集合；
- 单航班失败不得导致整个批次全部丢失（批量遍历逐航班 try/except）；
- 分批游标读取，不一次把完整批次读入内存。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from scat.materialization.aggregates import fetch_flight_feature_basis
from scat.materialization.contracts import (
    BatchStatisticsRecord,
    FlightFeaturesRecord,
    FlightPhaseSegmentEntry,
    FlightPhaseSegmentsRecord,
    PredictionMetricsRecord,
    PredictionMetricSummaryRecord,
)
from scat.materialization.repository import MaterializationRepository
from scat.prediction.alignment import HORIZONS_S, evaluate_flight
from scat.preprocessing.phases import (
    feature_bucket_seconds,
    interval_phase_seconds,
    label_trajectory,
    modal_phase,
)
from scat.repositories.flight_repository import SCAT_SOURCE_SYSTEM, ScatFlightRepository

# 物化算法版本：聚合口径变化时必须递增，旧版本结果不覆盖（M4 版本隔离）。
BATCH_STATISTICS_ALGORITHM_VERSION = "1"
# v2：阶段秒数按 ISO 文本解析（v1 因只认 datetime 而恒为 0）；高度由 FL 换算为米（v1 误存 FL）。
# v3：阶段秒数改为论文口径（三维 ENU RDP 分段 + 段内 vert 众数 + 六类→三桶），不再按 vert 分组区间求和。
FLIGHT_FEATURES_ALGORITHM_VERSION = "3"
# 段级阶段标注（M4-12）：RDP 分段 + 六类（takeoff_initial_climb/climb/high_cruise/midlow_level/descent/approach）。
FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION = "1"
# 预测指标物化（M5-05）：口径冻结于 docs/PREDICTION_EVALUATION.md。
# v1：scat-prediction-metrics-v1，经度插值把经差乘了两次（稀疏航路点会产出假经度）；
# v2：scat-prediction-metrics-v2，修正为沿最短弧推进，必须重算且旧 v1 行保留。
PREDICTION_METRICS_ALGORITHM_VERSION = "2"
# 指标汇总（M5-06）：按（批次, 时域, 分组维度, 分组值）聚合；聚合口径或上游指标版本变化时递增。
PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION = "2"

# 源数据版本：口径与 SCAT 导入数据快照绑定；数据重导后应递增。
# scat-import-2：用户按模型覆盖区裁剪原集合（13138 → 10637）后按此版本重新物化。
DEFAULT_SOURCE_VERSION = "scat-import-2"

# 一个 flight level = 100 英尺 = 30.48 米（与 ScatFlightService._flight_level_to_metres 一致）。
FLIGHT_LEVEL_TO_METRES = 30.48

# 阶段秒数来源：`scat.preprocessing.phases`（论文口径：RDP 分段 + 段内 vert 众数 + 六类），
# 本模块只负责六类→爬升/巡航/下降三桶映射与入库。


async def materialize_batch_statistics(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = BATCH_STATISTICS_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
) -> BatchStatisticsRecord:
    """聚合一个批次并幂等写入 scat_batch_statistics。"""
    statistics = await flight_repository.get_batch_statistics(batch=batch)
    if statistics is None:
        raise ValueError(f"批次不存在或无数据: {batch}")
    quality = await flight_repository.get_batch_data_quality(batch=batch)

    record = BatchStatisticsRecord(
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at or datetime.now(UTC),
        batch=batch,
        flight_count=statistics["flight_count"],
        unique_callsign_count=statistics["unique_callsign_count"],
        aircraft_type_count=statistics["aircraft_type_count"],
        departure_airport_count=statistics["departure_airport_count"],
        arrival_airport_count=statistics["arrival_airport_count"],
        total_track_points=statistics["total_track_points"],
        average_track_points=statistics["average_track_points"],
        minimum_track_points=statistics["minimum_track_points"],
        maximum_track_points=statistics["maximum_track_points"],
        flights_with_prediction=statistics["flights_with_prediction"],
        total_prediction_snapshots=statistics["total_prediction_snapshots"],
        missing_callsign_count=quality["missing_callsign_count"],
        missing_aircraft_type_count=quality["missing_aircraft_type_count"],
        missing_departure_airport_count=quality["missing_departure_airport_count"],
        missing_arrival_airport_count=quality["missing_arrival_airport_count"],
        missing_iobt_count=quality["missing_iobt_count"],
        empty_trajectory_count=quality["empty_trajectory_count"],
        flights_without_prediction_count=quality["flights_without_prediction_count"],
    )
    await materialization_repository.upsert_record("batch_statistics", record)
    return record


async def materialize_flight_features(
    *,
    batch: str,
    external_id: str | int,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_FEATURES_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
) -> FlightFeaturesRecord:
    """聚合单航班特征并幂等写入 scat_flight_features。"""
    basis = await fetch_flight_feature_basis(
        flight_repository.collection,
        batch=batch,
        external_id=external_id,
    )
    if basis is None or int(basis.get("point_count") or 0) < 1:
        raise ValueError(f"航班无有效轨迹点: {batch}/{external_id}")

    phase_rows = await flight_repository.get_trajectory_points_for_phases(
        batch=batch, external_id=external_id
    )
    phases = await flight_repository.get_trajectory_phase_stats(
        batch=batch, external_id=external_id
    )
    record = build_flight_features_record(
        batch=batch,
        external_id=external_id,
        basis=basis,
        phase_rows=phase_rows,
        phases=phases,
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at,
    )
    await materialization_repository.upsert_record("flight_features", record)
    return record


def build_flight_features_record(
    *,
    batch,
    external_id,
    basis,
    phase_rows,
    phases,
    source_version=DEFAULT_SOURCE_VERSION,
    algorithm_version=FLIGHT_FEATURES_ALGORITHM_VERSION,
    generated_at=None,
):
    """Pure M4 record builder shared by the legacy repository and frozen S5 inputs."""
    phase_labels, _ = label_trajectory(phase_points_from_rows(phase_rows))
    # 按点区间累计（总和 = 轨迹跨度），不受 RDP 单点段与边界点归属影响。
    phase_seconds = feature_bucket_seconds(interval_phase_seconds(phase_labels))

    speed_sum = 0.0
    speed_points = 0
    maximum_speed = 0.0
    minimum_altitude: float | None = None
    maximum_altitude: float | None = None
    # 阶段秒数已按论文口径（RDP 分段）算出；下面的聚合只提供地速与高度范围。
    for row in phases or []:
        count = int(row.get("point_count") or 0)
        average_speed = row.get("average_ground_speed_mps")
        if count > 0 and isinstance(average_speed, (int, float)):
            speed_sum += float(average_speed) * count
            speed_points += count
        maximum = row.get("maximum_ground_speed_mps")
        if isinstance(maximum, (int, float)):
            maximum_speed = max(maximum_speed, float(maximum))
        for bound, target in (("min_flight_level", "min"), ("max_flight_level", "max")):
            value = row.get(bound)
            if not isinstance(value, (int, float)):
                continue
            # 阶段行的 flight level 以 100 英尺为单位，入库统一换算为米。
            altitude = float(value) * FLIGHT_LEVEL_TO_METRES
            if target == "min":
                if minimum_altitude is None or altitude < minimum_altitude:
                    minimum_altitude = altitude
            elif maximum_altitude is None or altitude > maximum_altitude:
                maximum_altitude = altitude

    record = FlightFeaturesRecord(
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at or datetime.now(UTC),
        batch=batch,
        external_id=str(external_id),
        aircraft_type=basis.get("aircraft_type"),
        duration_s=float(basis["duration_s"]),
        distance_km=float(basis["distance_m"]) / 1000.0,
        point_count=int(basis["point_count"]),
        climb_seconds=phase_seconds["climb"],
        cruise_seconds=phase_seconds["cruise"],
        descent_seconds=phase_seconds["descent"],
        average_ground_speed_mps=(speed_sum / speed_points) if speed_points else 0.0,
        maximum_ground_speed_mps=maximum_speed,
        minimum_altitude_m=minimum_altitude,
        maximum_altitude_m=maximum_altitude,
    )
    return record


async def materialize_flight_phase_segments(
    *,
    batch: str,
    external_id: str | int,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
) -> FlightPhaseSegmentsRecord:
    """按论文口径分段并幂等写入 scat_flight_phase_segments（M4-12，段级标注）。"""
    raw_points = phase_points_from_rows(
        await flight_repository.get_trajectory_points_for_phases(
            batch=batch,
            external_id=external_id,
        )
    )
    if not raw_points:
        raise ValueError(f"航班无有效轨迹点: {batch}/{external_id}")
    phase_labels, segments = label_trajectory(raw_points)
    seconds = interval_phase_seconds(phase_labels)

    record = FlightPhaseSegmentsRecord(
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at or datetime.now(UTC),
        batch=batch,
        external_id=str(external_id),
        point_count=len(raw_points),
        segment_count=len(segments),
        dominant_phase=modal_phase(label.phase for label in phase_labels),
        phase_seconds={phase: round(value, 3) for phase, value in seconds.items() if value},
        segments=[
            FlightPhaseSegmentEntry(
                index=segment.index,
                start_time=segment.start_time,
                end_time=segment.end_time,
                point_count=segment.point_count,
                duration_s=round(segment.duration_s or 0.0, 3),
                mean_flight_level=segment.mean_flight_level,
                vertical_mode=segment.vertical_mode,
                phase=segment.phase,
            )
            for segment in segments
        ],
    )
    await materialization_repository.upsert_record("phase_segments", record)
    return record


async def materialize_batch_flight_features(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    skip_existing: bool = False,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_FEATURES_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """按批（或指定航班）遍历逐条物化航班特征（M4-05/M4-06：增量与失败隔离）。"""
    return await _materialize_batch(
        kind="flight_features",
        unit=materialize_flight_features,
        batch=batch,
        flight_repository=flight_repository,
        materialization_repository=materialization_repository,
        external_ids=external_ids,
        skip_existing=skip_existing,
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at,
        page_size=page_size,
        concurrency=concurrency,
        limit=limit,
        on_progress=on_progress,
    )


async def materialize_batch_flight_phase_segments(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    skip_existing: bool = False,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """按批（或指定航班）遍历逐条物化段级阶段标注（M4-12，复用 M4-05/M4-06 机制）。"""
    return await _materialize_batch(
        kind="phase_segments",
        unit=materialize_flight_phase_segments,
        batch=batch,
        flight_repository=flight_repository,
        materialization_repository=materialization_repository,
        external_ids=external_ids,
        skip_existing=skip_existing,
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at,
        page_size=page_size,
        concurrency=concurrency,
        limit=limit,
        on_progress=on_progress,
    )


async def materialize_prediction_metrics(
    *,
    batch: str,
    external_id: str | int,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = PREDICTION_METRICS_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    horizons: Sequence[float] = HORIZONS_S,
    include_excluded: bool = False,
) -> int:
    """评估一条航班并把有效样本的对齐结果幂等写入 scat_prediction_metrics（M5-05）。

    - 默认只写**有效样本**（计划原文：把有效样本写入指标集合）；`include_excluded=True`
      时连排除样本一起入库（`is_valid=false` + `exclusion_reason`，审计用，体量翻倍）；
    - 排除原因仍由 `scat.prediction.alignment.evaluate_flight` 的 `exclusions` 统计提供；
    - 航班存在但无预测快照时返回 0（不算失败）；航班不存在则报错（真实数据缺口）。
    """
    inputs = await flight_repository.get_evaluation_inputs(
        batch=batch,
        external_id=external_id,
    )
    if inputs is None:
        raise ValueError(f"航班不存在: {batch}/{external_id}")
    if not inputs.get("snapshots"):
        # 该航班没有预测快照：无样本可评估，不算失败（返回 0 条）
        return 0

    evaluation = evaluate_flight(
        plots=inputs.get("plots") or [],
        snapshots=inputs.get("snapshots") or [],
        horizons=horizons,
    )
    moment = generated_at or datetime.now(UTC)
    records: list[PredictionMetricsRecord] = []
    for comparison in evaluation.comparisons:
        if not comparison.valid and not include_excluded:
            continue
        record = PredictionMetricsRecord(
            source_version=source_version,
            algorithm_version=algorithm_version,
            generated_at=moment,
            batch=batch,
            external_id=str(external_id),
            snapshot_time=comparison.snapshot_time,
            horizon_s=float(comparison.horizon_s),
            target_time=comparison.target_time,
            horizontal_error_m=comparison.horizontal_error_m,
            vertical_error_m=comparison.vertical_error_m,
            along_track_error_m=comparison.along_track_m,
            cross_track_error_m=comparison.cross_track_m,
            is_valid=comparison.valid,
            exclusion_reason=(
                comparison.exclusion_reason.value
                if comparison.exclusion_reason is not None
                else None
            ),
        )
        records.append(record)
    return await materialization_repository.upsert_records("prediction_metrics", records)


async def materialize_batch_prediction_metrics(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    skip_existing: bool = False,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = PREDICTION_METRICS_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """按批（或指定航班）遍历逐条物化预测指标（M5-05，复用 M4-05/M4-06 机制）。

    增量跳过以“航班是否已有当前版本记录”为准；若某航班上轮被中断（只写了部分快照），
    用 `--force` 或 `--external-id` 重算该航班即可补齐（写入按（快照，时域）幂等 upsert）。
    预测时域固定为冻结口径 `HORIZONS_S`（D-16），不开放覆盖。
    """
    return await _materialize_batch(
        kind="prediction_metrics",
        unit=materialize_prediction_metrics,
        batch=batch,
        flight_repository=flight_repository,
        materialization_repository=materialization_repository,
        external_ids=external_ids,
        skip_existing=skip_existing,
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at,
        page_size=page_size,
        concurrency=concurrency,
        limit=limit,
        on_progress=on_progress,
    )


async def materialize_prediction_metric_summaries(
    *,
    batch: str,
    materialization_repository: MaterializationRepository,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    group_bys: Sequence[str] = ("overall", "phase", "aircraft_type"),
) -> int:
    """按批次聚合预测指标并幂等写入 scat_prediction_metric_summaries（M5-06，批次级任务）。

    - 先按（航班, 时域）聚合有效样本的和/平方和（Mongo 侧），再用阶段/机型映射做分组；
    - 分组维度：`overall`（全批次）、`phase`（航段主导阶段）、`aircraft_type`（机型），
      每个分组都带 `sample_count` 与 `flight_count`（D-18“分组必须带样本量”）；
    - MAE/RMSE 由平方和精确得出；**分位数（median/P90/P95）待下轮补**（需样本分布或 `$percentile`）；
    - 返回写入的汇总记录数。
    """
    rows = await materialization_repository.collection("prediction_metrics").aggregate(
        [
            {
                "$match": {
                    "batch": batch,
                    "source_version": source_version,
                    "algorithm_version": PREDICTION_METRICS_ALGORITHM_VERSION,
                    "is_valid": True,
                }
            },
            {
                "$group": {
                    "_id": {"external_id": "$external_id", "horizon_s": "$horizon_s"},
                    "count": {"$sum": 1},
                    "sum_h": {"$sum": "$horizontal_error_m"},
                    "sum_h2": {
                        "$sum": {"$multiply": ["$horizontal_error_m", "$horizontal_error_m"]}
                    },
                    "sum_abs_v": {"$sum": {"$abs": "$vertical_error_m"}},
                    "sum_v2": {"$sum": {"$multiply": ["$vertical_error_m", "$vertical_error_m"]}},
                    "sum_abs_along": {"$sum": {"$abs": "$along_track_error_m"}},
                    "sum_abs_cross": {"$sum": {"$abs": "$cross_track_error_m"}},
                }
            },
        ]
    )
    per_flight = await rows.to_list(length=None)
    if not per_flight:
        return 0

    phase_map = await _value_map(
        materialization_repository,
        "phase_segments",
        batch=batch,
        field="dominant_phase",
        source_version=source_version,
        algorithm_version=FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    )
    aircraft_map = await _value_map(
        materialization_repository,
        "flight_features",
        batch=batch,
        field="aircraft_type",
        source_version=source_version,
        algorithm_version=FLIGHT_FEATURES_ALGORITHM_VERSION,
    )

    percentiles = await _sample_percentiles(
        materialization_repository, batch=batch, source_version=source_version
    )
    records = build_prediction_metric_summary_records(
        per_flight=per_flight,
        phase_map=phase_map,
        aircraft_map=aircraft_map,
        percentiles=percentiles,
        batch=batch,
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated_at,
        group_bys=group_bys,
    )
    return await materialization_repository.upsert_records("prediction_metric_summaries", records)


def build_prediction_metric_summary_records(
    *,
    per_flight,
    phase_map,
    aircraft_map,
    percentiles,
    batch,
    source_version=DEFAULT_SOURCE_VERSION,
    algorithm_version=PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    generated_at=None,
    group_bys=("overall", "phase", "aircraft_type"),
):
    """Pure frozen M5 summary arithmetic; callers supply explicit distribution estimates."""
    buckets: dict[tuple[str, str, float], dict[str, float]] = {}
    for row in per_flight:
        identifier = str((row.get("_id") or {}).get("external_id"))
        horizon_s = float((row.get("_id") or {}).get("horizon_s") or 0.0)
        memberships: list[tuple[str, str]] = [("overall", "all")]
        if "phase" in group_bys:
            memberships.append(("phase", str(phase_map.get(identifier) or "unknown")))
        if "aircraft_type" in group_bys:
            memberships.append(("aircraft_type", str(aircraft_map.get(identifier) or "unknown")))
        for group_by, group_value in memberships:
            bucket = buckets.setdefault(
                (group_by, group_value, horizon_s),
                {
                    "count": 0.0,
                    "flights": 0.0,
                    "sum_h": 0.0,
                    "sum_h2": 0.0,
                    "sum_abs_v": 0.0,
                    "sum_v2": 0.0,
                    "sum_abs_along": 0.0,
                    "sum_abs_cross": 0.0,
                },
            )
            bucket["count"] += float(row.get("count") or 0)
            bucket["flights"] += 1.0
            for key in ("sum_h", "sum_h2", "sum_abs_v", "sum_v2", "sum_abs_along", "sum_abs_cross"):
                bucket[key] += float(row.get(key) or 0.0)

    moment = generated_at or datetime.now(UTC)
    records: list[PredictionMetricSummaryRecord] = []
    for (group_by, group_value, horizon_s), bucket in buckets.items():
        count = bucket["count"]
        if count <= 0:
            continue
        horizontal_percentiles = (
            percentiles.get((horizon_s, "horizontal")) if group_by == "overall" else None
        )
        vertical_percentiles = (
            percentiles.get((horizon_s, "vertical")) if group_by == "overall" else None
        )
        record = PredictionMetricSummaryRecord(
            source_version=source_version,
            algorithm_version=algorithm_version,
            generated_at=moment,
            batch=batch,
            horizon_s=horizon_s,
            group_by=group_by,
            group_value=group_value,
            sample_count=int(count),
            flight_count=int(bucket["flights"]),
            horizontal_mae_m=bucket["sum_h"] / count,
            horizontal_rmse_m=(bucket["sum_h2"] / count) ** 0.5,
            vertical_mae_m=bucket["sum_abs_v"] / count,
            vertical_rmse_m=(bucket["sum_v2"] / count) ** 0.5,
            along_track_mae_m=bucket["sum_abs_along"] / count,
            cross_track_mae_m=bucket["sum_abs_cross"] / count,
            horizontal_median_m=(horizontal_percentiles or [None, None, None])[0],
            horizontal_p90_m=(horizontal_percentiles or [None, None, None])[1],
            horizontal_p95_m=(horizontal_percentiles or [None, None, None])[2],
            vertical_median_m=(vertical_percentiles or [None, None, None])[0],
            vertical_p90_m=(vertical_percentiles or [None, None, None])[1],
            vertical_p95_m=(vertical_percentiles or [None, None, None])[2],
            percentile_method="approximate" if horizontal_percentiles else None,
        )
        records.append(record)
    return records


async def _sample_percentiles(
    materialization_repository: MaterializationRepository,
    *,
    batch: str,
    source_version: str,
) -> dict[tuple[float, str], list[float | None]]:
    """样本级分位数（median/P90/P95）：按 Mongo `$percentile` 分组计算（失败则返回空，不影响 MAE/RMSE）。

    `$percentile` 的输入必须是字段（不支持表达式），因此在 `$project` 里先做 `$abs`；
    只对 `overall` 分组产出（阶段/机型分组待下轮）。
    """
    try:
        cursor = await materialization_repository.collection("prediction_metrics").aggregate(
            [
                {
                    "$match": {
                        "batch": batch,
                        "source_version": source_version,
                        "algorithm_version": PREDICTION_METRICS_ALGORITHM_VERSION,
                        "is_valid": True,
                    }
                },
                {
                    "$project": {
                        "horizon_s": 1,
                        "horizontal_error_m": 1,
                        "abs_v": {"$abs": "$vertical_error_m"},
                    }
                },
                {
                    "$group": {
                        "_id": "$horizon_s",
                        "h": {
                            "$percentile": {
                                "input": "$horizontal_error_m",
                                "p": [0.5, 0.9, 0.95],
                                "method": "approximate",
                            }
                        },
                        "v": {
                            "$percentile": {
                                "input": "$abs_v",
                                "p": [0.5, 0.9, 0.95],
                                "method": "approximate",
                            }
                        },
                    }
                },
            ]
        )
        rows = await cursor.to_list(length=None)
        mapping: dict[tuple[float, str], list[float | None]] = {}
        for row in rows:
            horizon_s = float(row.get("_id") or 0.0)
            for key, label in (("h", "horizontal"), ("v", "vertical")):
                values = row.get(key)
                if isinstance(values, list) and len(values) == 3:
                    mapping[(horizon_s, label)] = [
                        None if value is None else float(value) for value in values
                    ]
        return mapping
    except Exception:  # noqa: BLE001 - 分位数不可用/响应异常时不影响主指标
        return {}


async def _value_map(
    materialization_repository: MaterializationRepository,
    kind: str,
    *,
    batch: str,
    field: str,
    source_version: str,
    algorithm_version: str,
) -> dict[str, Any]:
    """读取某物化集合的 external_id → 字段值映射（分组用；有界）。"""
    cursor = await materialization_repository.collection(kind).aggregate(
        [
            {
                "$match": {
                    "batch": batch,
                    "source_version": source_version,
                    "algorithm_version": algorithm_version,
                }
            },
            {"$project": {"_id": 0, "external_id": 1, field: 1}},
        ]
    )
    mapping: dict[str, Any] = {}
    for document in await cursor.to_list(length=None):
        identifier = document.get("external_id")
        if identifier is not None:
            mapping[str(identifier)] = document.get(field)
    return mapping


async def _materialize_batch(
    *,
    kind: str,
    unit: Callable[..., Awaitable[Any]],
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    skip_existing: bool = False,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_FEATURES_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """按批（或指定航班）遍历逐条物化；单航班失败不影响其它航班。

    - 使用 _id 游标分页，每页只取 external_id 投影，不把完整批次读入内存；
    - skip_existing=True 时跳过已有「相同 source_version + algorithm_version」结果的
      航班，使重复执行/中断后重跑只补算缺失部分（M4-06 增量重算）；
    - on_progress 在每个单位处理完成后回调（含本单位的 failure 明细），由任务层写进度。
    """
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency 必须在 1 到 16 之间")
    target_ids = [str(value) for value in external_ids] if external_ids is not None else None
    existing: set[str] = set()
    if skip_existing:
        existing = await materialization_repository.existing_values(
            kind,
            "external_id",
            {
                "batch": batch,
                "source_version": source_version,
                "algorithm_version": algorithm_version,
            },
        )

    succeeded = 0
    skipped = 0
    failed: list[dict[str, str]] = []
    processed = 0
    stream: AsyncIterator[str] = (
        _aiter(target_ids)
        if target_ids is not None
        else iter_batch_external_ids(flight_repository, batch=batch, page_size=page_size)
    )
    pending: list[str] = []

    async def flush() -> None:
        nonlocal processed, succeeded
        if not pending:
            return
        identifiers = list(pending)
        pending.clear()
        errors = await asyncio.gather(
            *(
                _materialize_one(
                    unit=unit,
                    batch=batch,
                    external_id=external_id,
                    flight_repository=flight_repository,
                    materialization_repository=materialization_repository,
                    source_version=source_version,
                    algorithm_version=algorithm_version,
                    generated_at=generated_at,
                )
                for external_id in identifiers
            )
        )
        # gather preserves input order, so progress callbacks remain deterministic.
        for external_id, error in zip(identifiers, errors, strict=True):
            if error is None:
                succeeded += 1
            else:
                failed.append({"external_id": external_id, "error": error})
            processed += 1
            if on_progress is not None:
                update: dict[str, Any] = {
                    "processed": processed,
                    "succeeded": succeeded,
                    "failed": len(failed),
                    "skipped": skipped,
                }
                if error is not None:
                    update["failure"] = {"external_id": external_id, "error": error}
                await on_progress(update)

    async for external_id in stream:
        if limit is not None and processed + len(pending) >= limit:
            break
        if skip_existing and external_id in existing:
            skipped += 1
            continue
        pending.append(external_id)
        if len(pending) >= concurrency:
            await flush()
    await flush()

    return {
        "batch": batch,
        "succeeded_count": succeeded,
        "failed_count": len(failed),
        "skipped_count": skipped,
        "failed": failed[:100],
    }


async def iter_batch_external_ids(
    flight_repository: ScatFlightRepository,
    *,
    batch: str,
    page_size: int = 200,
) -> AsyncIterator[str]:
    """按 _id 游标分页产出批次内的航班 external_id（有界内存）。"""
    if page_size < 1:
        raise ValueError("page_size 必须大于 0")
    normalized_batch = batch.strip()
    if not normalized_batch:
        raise ValueError("batch 不能为空")

    last_id: Any = None
    while True:
        page_filter: dict[str, Any] = {
            "source.system": SCAT_SOURCE_SYSTEM,
            "source.batch": normalized_batch,
        }
        if last_id is not None:
            page_filter["_id"] = {"$gt": last_id}
        cursor = (
            flight_repository.collection.find(page_filter, {"source.external_id": 1})
            .sort("_id", 1)
            .limit(page_size)
        )
        page = await cursor.to_list(length=page_size)
        if not page:
            return
        for document in page:
            external_id = (document.get("source") or {}).get("external_id")
            if external_id is not None:
                yield str(external_id)
        last_id = page[-1]["_id"]


async def _aiter(values: Sequence[str]) -> AsyncIterator[str]:
    for value in values:
        yield value


async def _materialize_one(
    *,
    unit: Callable[..., Awaitable[Any]],
    batch: str,
    external_id: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    source_version: str,
    algorithm_version: str,
    generated_at: datetime | None,
) -> str | None:
    """物化单个航班；返回 None 表示成功，否则返回截断后的失败原因。"""
    try:
        await unit(
            batch=batch,
            external_id=external_id,
            flight_repository=flight_repository,
            materialization_repository=materialization_repository,
            source_version=source_version,
            algorithm_version=algorithm_version,
            generated_at=generated_at,
        )
    except Exception as error:  # noqa: BLE001 - 单航班失败隔离，不阻断其它航班
        return str(error)[:300]
    return None


def phase_points_from_rows(rows: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """仓库点行 → `label_trajectory` 的入参（字段名映射集中在此）。"""
    return [
        {
            "time": row.get("timestamp"),
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "flight_level": row.get("measured_flight_level"),
            "vertical_mode": row.get("vertical_mode"),
        }
        for row in rows or []
        if row.get("lat") is not None and row.get("lon") is not None
    ]


__all__ = [
    "BATCH_STATISTICS_ALGORITHM_VERSION",
    "DEFAULT_SOURCE_VERSION",
    "FLIGHT_FEATURES_ALGORITHM_VERSION",
    "FLIGHT_LEVEL_TO_METRES",
    "FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION",
    "PREDICTION_METRICS_ALGORITHM_VERSION",
    "PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION",
    "iter_batch_external_ids",
    "materialize_batch_flight_features",
    "materialize_batch_flight_phase_segments",
    "materialize_batch_prediction_metrics",
    "materialize_batch_statistics",
    "materialize_flight_features",
    "materialize_flight_phase_segments",
    "materialize_prediction_metric_summaries",
    "materialize_prediction_metrics",
    "phase_points_from_rows",
]
