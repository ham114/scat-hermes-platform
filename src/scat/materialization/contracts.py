"""M4 物化计算派生集合契约（M4-01/02/03）。

- M4-01：五个派生集合的 Pydantic 模型；
- M4-02：唯一键、查询索引与保留策略声明；
- M4-03：每条结果记录 source_version / algorithm_version / generated_at。

口径约束（与计划 M4 一致）：
- 唯一性 = 业务键 + source_version + algorithm_version：算法版本或数据版本变化
  不覆盖旧结果，多版本共存；「最新有效结果」由 generated_at 排序决定（M4-09 读取策略）；
- 原始集合只读：物化任务不得写原始集合；
- 保留策略：统计/特征/指标默认永久（expires_at=None，可配置 TTL）；
  任务记录 30 天后过期（TTL 索引兜底，M4-10 再实现主动清理）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scat.prediction.alignment import PREDICTION_METRIC_VERSION

MATERIALIZATION_JOB_RETENTION_DAYS = 30

# 任务状态：partial = 部分单位失败但其余结果已写入（重试据此只补算 failed_targets）。
MaterializationJobStatus = Literal["pending", "running", "succeeded", "partial", "failed"]


class MaterializedRecord(BaseModel):
    """所有派生集合记录的公共字段（M4-03）。"""

    model_config = ConfigDict(extra="forbid")

    source_version: str = Field(min_length=1, max_length=100)
    algorithm_version: str = Field(min_length=1, max_length=100)
    generated_at: datetime = Field(description="UTC 生成时间")
    expires_at: datetime | None = Field(default=None, description="TTL 过期时间；None=永久")


class BatchStatisticsRecord(MaterializedRecord):
    """scat_batch_statistics：批次总体统计与字段覆盖（口径对齐 BatchOverview.statistics）。"""

    batch: str = Field(min_length=1, max_length=100)
    flight_count: int = Field(ge=0)
    unique_callsign_count: int = Field(ge=0)
    aircraft_type_count: int = Field(ge=0)
    departure_airport_count: int = Field(ge=0)
    arrival_airport_count: int = Field(ge=0)
    total_track_points: int = Field(ge=0)
    average_track_points: float = Field(ge=0)
    minimum_track_points: int = Field(ge=0)
    maximum_track_points: int = Field(ge=0)
    flights_with_prediction: int = Field(ge=0)
    total_prediction_snapshots: int = Field(ge=0)
    # 字段覆盖（口径对齐 BatchDataQuality）：身份/IOBT/轨迹/预测缺失计数
    missing_callsign_count: int = Field(ge=0)
    missing_aircraft_type_count: int = Field(ge=0)
    missing_departure_airport_count: int = Field(ge=0)
    missing_arrival_airport_count: int = Field(ge=0)
    missing_iobt_count: int = Field(ge=0)
    empty_trajectory_count: int = Field(ge=0)
    flights_without_prediction_count: int = Field(ge=0)


class RouteStatisticsRecord(MaterializedRecord):
    """scat_route_statistics：航线（起降机场对）统计。"""

    batch: str = Field(min_length=1, max_length=100)
    adep: str = Field(min_length=1, max_length=10)
    ades: str = Field(min_length=1, max_length=10)
    flight_count: int = Field(ge=0)
    aircraft_type_count: int = Field(ge=0)
    total_track_points: int = Field(ge=0)
    average_duration_s: float = Field(ge=0)


class FlightFeaturesRecord(MaterializedRecord):
    """scat_flight_features：单航班时长、距离、点数与阶段特征。"""

    batch: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=100)
    aircraft_type: str | None = Field(default=None, max_length=50)
    duration_s: float = Field(ge=0)
    distance_km: float = Field(ge=0)
    point_count: int = Field(ge=0)
    climb_seconds: float = Field(ge=0)
    cruise_seconds: float = Field(ge=0)
    descent_seconds: float = Field(ge=0)
    average_ground_speed_mps: float = Field(ge=0)
    maximum_ground_speed_mps: float = Field(ge=0)
    minimum_altitude_m: float | None = Field(default=None)
    maximum_altitude_m: float | None = Field(default=None)


class FlightPhaseSegmentEntry(BaseModel):
    """单条 RDP 段的阶段标注（M4-12，论文口径）。"""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, description="段序号（自左向右）")
    start_time: datetime | None = Field(default=None)
    end_time: datetime | None = Field(default=None)
    point_count: int = Field(ge=1)
    duration_s: float = Field(ge=0)
    mean_flight_level: float | None = Field(default=None)
    vertical_mode: int = Field(ge=0, le=3, description="段内 I062/200.vert 众数")
    phase: str = Field(min_length=1, max_length=32, description="段主导六类阶段")


class FlightPhaseSegmentsRecord(MaterializedRecord):
    """scat_flight_phase_segments：单航班 RDP 分段与六类阶段标注。

    `phase_seconds` 按点区间归属累计（总和等于轨迹跨度）；`dominant_phase` 为整航班
    众数阶段，供 M5 分组汇总与 M6 窗口标签使用。
    """

    batch: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=100)
    point_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    dominant_phase: str | None = Field(default=None, max_length=32)
    phase_seconds: dict[str, float] = Field(default_factory=dict)
    segments: list[FlightPhaseSegmentEntry] = Field(default_factory=list)


class PredictionMetricsRecord(MaterializedRecord):
    """scat_prediction_metrics：每航班、快照、预测时域的误差（M5-05）。

    - 有效样本写入误差值；被排除的样本也入库（`is_valid=false` + `exclusion_reason`）
      以便审计，聚合统计时必须按 `is_valid=true` 过滤；
    - `metric_version` 记录口径版本（M5-01 冻结；v2 修正经度插值，见 docs/PREDICTION_EVALUATION.md）；
    - `horizontal_error_m` 为绝对距离（≥0），`vertical_error_m` 为带符号差值（actual − predicted）；
    - 相对 v1：误差字段放宽为可空、新增沿航向/横向误差、有效性/排除原因与口径版本。
    """

    batch: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=100)
    snapshot_time: datetime = Field(description="UTC 快照时间")
    horizon_s: float = Field(ge=0, description="预测时域（相对快照的秒数）")
    target_time: datetime | None = Field(default=None, description="对齐时刻 t* = snapshot + H")
    horizontal_error_m: float | None = Field(default=None, ge=0)
    vertical_error_m: float | None = Field(
        default=None,
        description="米；正 = 实飞高于预测（actual − predicted，D-17），可为负",
    )
    along_track_error_m: float | None = Field(default=None)
    cross_track_error_m: float | None = Field(default=None)
    time_error_s: float = Field(default=0.0, ge=0)
    point_count: int = Field(default=1, ge=1)
    is_valid: bool = Field(default=True)
    exclusion_reason: str | None = Field(default=None, max_length=64)
    metric_version: str = Field(
        default=PREDICTION_METRIC_VERSION,
        min_length=1,
        max_length=100,
    )


class PredictionMetricSummaryRecord(MaterializedRecord):
    """scat_prediction_metric_summaries：按（批次, 时域, 分组维度, 分组值）聚合的指标汇总（M5-06）。

    分组维度（D-18：按快照主口径，分组必带样本量）：
    - `overall`：整批次（`group_value` 固定为 `all`）；
    - `phase`：按航段主导阶段（`FlightPhaseSegmentsRecord.dominant_phase`）；
    - `aircraft_type`：按机型（`FlightFeaturesRecord.aircraft_type`）。

    MAE/RMSE 由样本级聚合得出；分位数（median/P90/P95）由样本级 `$percentile` 给出（当前只对
    `overall` 分组计算，阶段/机型分组待下轮）。
    """

    batch: str = Field(min_length=1, max_length=100)
    horizon_s: float = Field(ge=0, description="预测时域（秒，冻结口径 60/180/300/600/900/1200）")
    group_by: str = Field(min_length=1, max_length=32, description="overall|phase|aircraft_type")
    group_value: str = Field(min_length=1, max_length=100, description="分组值；overall 固定为 all")
    sample_count: int = Field(ge=0, description="参与统计的有效样本数")
    flight_count: int = Field(ge=0, description="参与统计的航班数")
    horizontal_mae_m: float | None = Field(default=None, ge=0)
    horizontal_rmse_m: float | None = Field(default=None, ge=0)
    vertical_mae_m: float | None = Field(default=None, ge=0)
    vertical_rmse_m: float | None = Field(default=None, ge=0)
    along_track_mae_m: float | None = Field(default=None, ge=0)
    cross_track_mae_m: float | None = Field(default=None, ge=0)
    horizontal_median_m: float | None = Field(default=None, ge=0)
    horizontal_p90_m: float | None = Field(default=None, ge=0)
    horizontal_p95_m: float | None = Field(default=None, ge=0)
    vertical_median_m: float | None = Field(default=None, ge=0)
    vertical_p90_m: float | None = Field(default=None, ge=0)
    vertical_p95_m: float | None = Field(default=None, ge=0)
    percentile_method: str | None = Field(
        default=None,
        max_length=32,
        description="分位数算法（Mongo $percentile 的 method，当前为 approximate）；None=未计算",
    )
    metric_version: str = Field(
        default=PREDICTION_METRIC_VERSION,
        min_length=1,
        max_length=100,
    )


class MaterializationJobRecord(MaterializedRecord):
    """scat_materialization_jobs：任务状态、进度、版本与错误。

    `failed_targets` 保存失败单位的业务标识（航班 external_id 等），重试任务据此
    只重算失败部分；单个单位失败不改变其它单位的成功结果（M4 局部失败约束）。
    """

    job_id: str = Field(min_length=1, max_length=100)
    owner_user_id: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str = Field(description="batch_statistics|route_statistics|flight_features|prediction_metrics")
    batch: str = Field(min_length=1, max_length=100)
    status: MaterializationJobStatus = Field(
        default="pending",
        description="partial=部分单位失败，其余结果已写入",
    )
    progress: float = Field(default=0, ge=0, le=1)
    total_count: int = Field(default=0, ge=0, description="本次计划处理的单位数；0=未知")
    processed_count: int = Field(default=0, ge=0)
    failed_targets: list[str] = Field(default_factory=list)
    failure_detail: dict[str, str] = Field(
        default_factory=dict,
        description="失败单位 → 截断后的原因",
    )
    error_message: str | None = None
    retry_of: str | None = Field(default=None, description="重试来源任务 ID")

    @model_validator(mode="after")
    def apply_retention(self) -> MaterializationJobRecord:
        if self.expires_at is None:
            self.expires_at = self.generated_at + timedelta(
                days=MATERIALIZATION_JOB_RETENTION_DAYS
            )
        return self


# ---------------------------------------------------------------------------
# M4-02：唯一键（业务键 + 版本）、查询索引与保留策略声明。
# 这些声明由物化任务的建索引代码消费，也是契约测试的校验对象。
# ---------------------------------------------------------------------------

MATERIALIZATION_COLLECTIONS: dict[str, str] = {
    "batch_statistics": "scat_batch_statistics",
    "route_statistics": "scat_route_statistics",
    "flight_features": "scat_flight_features",
    "phase_segments": "scat_flight_phase_segments",
    "prediction_metrics": "scat_prediction_metrics",
    "prediction_metric_summaries": "scat_prediction_metric_summaries",
    "jobs": "scat_materialization_jobs",
}

# 业务键：决定「同一实体」的字段（不含版本字段）。
MATERIALIZATION_BUSINESS_KEYS: dict[str, list[str]] = {
    "batch_statistics": ["batch"],
    "route_statistics": ["batch", "adep", "ades"],
    "flight_features": ["batch", "external_id"],
    "phase_segments": ["batch", "external_id"],
    "prediction_metrics": ["batch", "external_id", "snapshot_time", "horizon_s"],
    "prediction_metric_summaries": ["batch", "horizon_s", "group_by", "group_value"],
    "jobs": ["job_id"],
}

# 版本字段：业务键 + 版本字段组成唯一索引；版本变化共存、不覆盖。
MATERIALIZATION_VERSION_KEYS: list[str] = ["source_version", "algorithm_version"]

# 查询索引（含唯一键自身之外的常用查询路径；前缀顺序 = 字段顺序）。
# 注意：TTL 字段不列在这里，由 MATERIALIZATION_TTL_INDEXES 单独建 TTL 索引——
# 同一个 key 不能同时存在普通索引与 TTL 索引（MongoDB IndexOptionsConflict）。
MATERIALIZATION_INDEXES: dict[str, list[list[str]]] = {
    "batch_statistics": [["batch", "generated_at"]],
    "route_statistics": [["batch", "adep"], ["adep", "ades"]],
    "flight_features": [["batch", "aircraft_type"]],
    "phase_segments": [["batch", "dominant_phase"]],
    "prediction_metrics": [
        ["batch", "external_id", "snapshot_time"],
        ["batch", "horizontal_error_m"],
    ],
    "prediction_metric_summaries": [["batch", "group_by", "horizon_s"]],
    "jobs": [["status", "generated_at"], ["kind", "batch", "generated_at"]],
}

# TTL 索引（保留策略的执行者）：jobs 的 expires_at 由 MaterializationJobRecord 计算。
MATERIALIZATION_TTL_INDEXES: dict[str, list[str]] = {
    "jobs": ["expires_at"],
}

# 保留策略：None = 永久；jobs 走 expires_at TTL（由 MaterializationJobRecord 计算）。
MATERIALIZATION_RETENTION_DAYS: dict[str, int | None] = {
    "batch_statistics": None,
    "route_statistics": None,
    "flight_features": None,
    "phase_segments": None,
    "prediction_metrics": None,
    "prediction_metric_summaries": None,
    "jobs": MATERIALIZATION_JOB_RETENTION_DAYS,
}

RECORD_MODELS: dict[str, type[MaterializedRecord]] = {
    "batch_statistics": BatchStatisticsRecord,
    "route_statistics": RouteStatisticsRecord,
    "flight_features": FlightFeaturesRecord,
    "phase_segments": FlightPhaseSegmentsRecord,
    "prediction_metrics": PredictionMetricsRecord,
    "prediction_metric_summaries": PredictionMetricSummaryRecord,
    "jobs": MaterializationJobRecord,
}

__all__ = [
    "MATERIALIZATION_BUSINESS_KEYS",
    "MATERIALIZATION_COLLECTIONS",
    "MATERIALIZATION_INDEXES",
    "MATERIALIZATION_JOB_RETENTION_DAYS",
    "MATERIALIZATION_RETENTION_DAYS",
    "MATERIALIZATION_TTL_INDEXES",
    "MATERIALIZATION_VERSION_KEYS",
    "RECORD_MODELS",
    "BatchStatisticsRecord",
    "FlightFeaturesRecord",
    "FlightPhaseSegmentEntry",
    "FlightPhaseSegmentsRecord",
    "MaterializationJobRecord",
    "MaterializationJobStatus",
    "MaterializedRecord",
    "PredictionMetricSummaryRecord",
    "PredictionMetricsRecord",
    "RouteStatisticsRecord",
]
