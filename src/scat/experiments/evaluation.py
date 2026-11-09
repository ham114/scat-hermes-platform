"""M6 评估协议 v1（`m6-eval-v1`）：纯计算部分。

协议（写入实验记录的 `protocol_version`）：

- 对每条航班的实飞轨迹按固定间隔取"观测截止点"（cutoff）：从 `min_observed_s`
  之后开始、步长 `stride_s`、单航班最多 `max_cutoffs_per_flight` 个（超出时均匀重采样）；
- 每个 cutoff：观测 = 截止点及之前的实飞点；目标时刻 = cutoff + 各档时域；
- 误差 = 预测点与实飞真值（线性插值，护栏 `max_actual_gap_s`）之间的球面距离（米）；
- 真值缺失（越界/缺口）计入排除统计；预测器拒绝（ValueError）计入 `predictor_rejected`；
- 聚合一律附样本量与覆盖航班数；分位数用线性插值（numpy 默认算法）。

与 M5（CFPL 基准）的关系：时域档位、球面距离函数、按样本聚合的口径一致，便于在
同一评估页对比；差异在于数据来源（模型从实飞观测外推 vs CFPL 计划航路）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, floor, sqrt

from scat.experiments.contracts import HorizonMetrics, PhaseHorizonMetrics, WorstSample
from scat.prediction.alignment import (
    DEFAULT_MAX_TRACK_GAP_S,
    HORIZONS_S,
    TrackPoint,
    haversine_metres,
    interpolate_track,
)
from scat.prediction.interfaces import PredictionRequest, TrajectoryPredictor

__all__ = [
    "EvaluationConfig",
    "SampleResult",
    "aggregate_phase_metrics",
    "aggregate_samples",
    "cutoff_times",
    "evaluate_track",
]


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """评估协议参数（写入实验记录 config，便于复现）。"""

    batch: str
    predictor_id: str
    horizons_s: tuple[float, ...] = tuple(float(value) for value in HORIZONS_S)
    stride_s: float = 60.0
    max_cutoffs_per_flight: int = 12
    min_observed_s: float = 60.0
    min_observed_points: int = 3
    max_actual_gap_s: float = DEFAULT_MAX_TRACK_GAP_S


@dataclass(frozen=True, slots=True)
class SampleResult:
    """一条有效样本：航班 + 截止时刻 + 时域 + 水平误差（米）。"""

    external_id: str
    cutoff_time: datetime
    horizon_s: float
    error_m: float


def cutoff_times(track: list[TrackPoint], config: EvaluationConfig) -> list[datetime]:
    """按协议选观测截止点；不足或没有未来段时返回空列表。"""
    if len(track) < config.min_observed_points:
        return []
    start_time = track[0].time + timedelta(seconds=config.min_observed_s)
    last_time = track[-1].time
    candidates: list[datetime] = []
    next_allowed = start_time
    for index, point in enumerate(track):
        if point.time < next_allowed or point.time >= last_time:
            continue
        if index + 1 < config.min_observed_points:
            continue
        candidates.append(point.time)
        next_allowed = point.time + timedelta(seconds=config.stride_s)
    if len(candidates) > config.max_cutoffs_per_flight:
        step = ceil(len(candidates) / config.max_cutoffs_per_flight)
        candidates = candidates[::step][: config.max_cutoffs_per_flight]
    return candidates


def evaluate_track(
    track: list[TrackPoint],
    predictor: TrajectoryPredictor,
    config: EvaluationConfig,
    *,
    external_id: str,
) -> tuple[list[SampleResult], dict[str, int]]:
    """在单条轨迹上按协议采样评估；返回（有效样本，排除原因计数）。"""
    samples: list[SampleResult] = []
    exclusions: Counter[str] = Counter()
    for cutoff in cutoff_times(track, config):
        observed = tuple(point for point in track if point.time <= cutoff)
        targets = tuple(cutoff + timedelta(seconds=horizon) for horizon in config.horizons_s)
        try:
            result = predictor.predict(PredictionRequest(observed=observed, target_times=targets))
        except ValueError:
            exclusions["predictor_rejected"] += 1
            continue
        for horizon, target, fix in zip(config.horizons_s, targets, result.fixes):
            actual, reason = interpolate_track(track, target, max_gap_s=config.max_actual_gap_s)
            if actual is None:
                exclusions[reason.value if reason is not None else "actual_unavailable"] += 1
                continue
            samples.append(
                SampleResult(
                    external_id=external_id,
                    cutoff_time=cutoff,
                    horizon_s=float(horizon),
                    error_m=haversine_metres(fix.lat, fix.lon, actual.lat, actual.lon),
                )
            )
    return samples, dict(exclusions)


def _percentile(values: list[float], quantile: float) -> float:
    """线性插值分位数（与 numpy 默认算法一致）。"""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return ordered[int(position)]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def aggregate_samples(
    samples: Iterable[SampleResult],
    *,
    horizons: Iterable[float],
    worst_limit: int = 10,
) -> tuple[list[HorizonMetrics], list[WorstSample]]:
    """按六档时域聚合（MAE/RMSE/中位数/P90/P95 + 样本量与覆盖航班），并取最差 Top-K。"""
    materialized = list(samples)
    by_horizon: dict[float, list[SampleResult]] = {float(horizon): [] for horizon in horizons}
    for sample in materialized:
        by_horizon.setdefault(float(sample.horizon_s), []).append(sample)

    metrics: list[HorizonMetrics] = []
    for horizon in sorted(by_horizon):
        items = by_horizon[horizon]
        if not items:
            metrics.append(HorizonMetrics(horizon_s=horizon, sample_count=0, flight_count=0))
            continue
        errors = [item.error_m for item in items]
        metrics.append(
            HorizonMetrics(
                horizon_s=horizon,
                sample_count=len(items),
                flight_count=len({item.external_id for item in items}),
                mae_m=sum(errors) / len(errors),
                rmse_m=sqrt(sum(error * error for error in errors) / len(errors)),
                median_m=_percentile(errors, 0.5),
                p90_m=_percentile(errors, 0.90),
                p95_m=_percentile(errors, 0.95),
            )
        )

    worst = sorted(materialized, key=lambda item: item.error_m, reverse=True)[:worst_limit]
    worst_samples = [
        WorstSample(
            external_id=item.external_id,
            cutoff_time=item.cutoff_time,
            horizon_s=item.horizon_s,
            error_m=item.error_m,
        )
        for item in worst
    ]
    return metrics, worst_samples


def aggregate_phase_metrics(
    samples: Iterable[SampleResult],
    *,
    phase_of: Callable[[SampleResult], str | None],
) -> tuple[list[PhaseHorizonMetrics], int]:
    """按（飞行阶段 × 时域）聚合分组表现；返回（指标行，未能归组的样本数）。

    阶段由调用方的 `phase_of` 解析（观测截止点所在段）；无法解析的样本计入
    第二返回值并由运行器写入 coverage，保证“分组表现缺多少样本”可解释。
    """
    buckets: dict[tuple[str, float], list[SampleResult]] = {}
    ungrouped = 0
    for sample in samples:
        phase = phase_of(sample)
        if not phase:
            ungrouped += 1
            continue
        buckets.setdefault((phase, float(sample.horizon_s)), []).append(sample)

    metrics: list[PhaseHorizonMetrics] = []
    for (phase, horizon), items in sorted(buckets.items()):
        errors = [item.error_m for item in items]
        metrics.append(
            PhaseHorizonMetrics(
                phase=phase,
                horizon_s=horizon,
                sample_count=len(items),
                flight_count=len({item.external_id for item in items}),
                mae_m=sum(errors) / len(errors),
                median_m=_percentile(errors, 0.5),
                p90_m=_percentile(errors, 0.90),
            )
        )
    return metrics, ungrouped
