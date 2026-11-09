"""统一预测对比的共享对齐核（GBAR 口径，D-22）。

**单一实现原则**：`GbarV23RegressionPackage` 与后续的预测对比包（`model_comparison`）
都通过本模块完成"预测点 → 实飞真值"的对齐，保证对比的**数据与条件完全一致**
（同一观测窗、同一预测时刻、同一真值匹配与容差），避免两套实现随时间漂移。

对齐规则（与 GBAR v23 原实现逐行一致）：

- 候选真值只接受带时区的 ISO 时间戳（朴素时间、非法项一律丢弃）；
- 每个预测点（`time_offset_seconds`）匹配其目标时刻的**最近**候选真值；
- 最近匹配与目标时刻相差超过 `max_time_diff_s`（默认 6.0 s，SCAT 约 5 s 采样的
  连续性护栏）视为无真值：期望值行置 None，且不产出对齐轨迹点；
- 数值只接受有限数（bool / NaN / Inf 视为缺失）；
- 轨迹点取前三个特征（lat, lon, altitude），altitude 缺失时省略该键。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from math import ceil, floor, isinf, isnan
from typing import Any

from scat.prediction.alignment import haversine_metres

DEFAULT_ALIGNMENT_TOLERANCE_S = 6.0

__all__ = [
    "DEFAULT_ALIGNMENT_TOLERANCE_S",
    "align_actual_future",
    "distance_distribution",
    "optional_number",
    "trajectory_ade_fde",
    "trajectory_horizontal_errors",
]


def optional_number(value: Any) -> float | None:
    """把候选值转成有限浮点数；非法值返回 None（与 GBAR 原实现一致）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if isnan(number) or isinf(number):
        return None
    return number


def align_actual_future(
    raw_candidates: Any,
    *,
    forecast_origin: datetime,
    offsets: list[float],
    feature_names: tuple[str, ...],
    max_time_diff_s: float = DEFAULT_ALIGNMENT_TOLERANCE_S,
) -> tuple[list[dict[str, float]], tuple[tuple[float | None, ...], ...]]:
    """按 GBAR 口径对齐真值；返回（对齐后的轨迹点，逐预测点的期望值行）。"""
    if not isinstance(raw_candidates, list):
        return [], ()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for item in raw_candidates:
        if not isinstance(item, dict) or not isinstance(item.get("timestamp"), str):
            continue
        try:
            timestamp = datetime.fromisoformat(item["timestamp"])
        except ValueError:
            continue
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            continue
        candidates.append((timestamp, item))

    aligned: list[dict[str, float]] = []
    expected_rows: list[tuple[float | None, ...]] = []
    for offset in offsets:
        target = forecast_origin.timestamp() + offset
        if not candidates:
            expected_rows.append(tuple(None for _ in feature_names))
            continue
        timestamp, item = min(
            candidates,
            key=lambda candidate: abs(candidate[0].timestamp() - target),
        )
        # SCAT 通常 5 秒采样：历史续段不连续时，不把远处的行当作真值。
        if abs(timestamp.timestamp() - target) > max_time_diff_s:
            expected_rows.append(tuple(None for _ in feature_names))
            continue
        expected = tuple(optional_number(item.get(name)) for name in feature_names)
        expected_rows.append(expected)
        if len(expected) >= 2 and expected[0] is not None and expected[1] is not None:
            point: dict[str, float] = {
                "time_step_s": float(offset),
                "lat": expected[0],
                "lon": expected[1],
            }
            altitude = expected[2] if len(expected) >= 3 else None
            if altitude is not None:
                point["altitude"] = altitude
            aligned.append(point)
    return aligned, tuple(expected_rows)


def trajectory_horizontal_errors(
    predicted: Sequence[tuple[float, float]],
    actual: Sequence[tuple[float, float]],
) -> tuple[float, ...]:
    """Return per-timestep horizontal Haversine errors in metres."""
    if not predicted or len(predicted) != len(actual):
        raise ValueError("predicted and actual trajectories must be non-empty and equal length")
    return tuple(
        haversine_metres(pred_lat, pred_lon, actual_lat, actual_lon)
        for (pred_lat, pred_lon), (actual_lat, actual_lon) in zip(
            predicted,
            actual,
            strict=True,
        )
    )


def trajectory_ade_fde(
    predicted: Sequence[tuple[float, float]],
    actual: Sequence[tuple[float, float]],
) -> tuple[float, float]:
    """Return horizontal ADE/FDE in metres for two equal, non-empty trajectories."""
    errors = trajectory_horizontal_errors(predicted, actual)
    return sum(errors) / len(errors), errors[-1]


def distance_distribution(values: Sequence[float]) -> tuple[float, float, float]:
    """Return mean/median/P90 using deterministic linear-interpolation percentiles."""
    if not values:
        raise ValueError("distance distribution requires at least one value")
    if any(value < 0 or isinf(value) or isnan(value) for value in values):
        raise ValueError("distance distribution values must be finite and non-negative")
    ordered = sorted(float(value) for value in values)

    def percentile(quantile: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * quantile
        lower = floor(position)
        upper = ceil(position)
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    return sum(ordered) / len(ordered), percentile(0.5), percentile(0.9)
