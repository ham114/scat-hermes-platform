"""M5-02/M5-03 轨迹对齐与误差计算（口径见 docs/PREDICTION_EVALUATION.md v1）。

纯计算模块：不访问数据库、不写任何集合，便于用手工构造轨迹做单测。
核心约定：
- 时间：SCAT 存无时区 ISO-8601 文本，统一按 UTC 解析；
- 时域：t* = 快照时刻 + H（H ∈ 1/3/5/10/15/20 分钟），预测与实飞都线性插值到 t*；
- 经度插值：沿最短弧推进（跨 ±180° 不绕行），结果折回 [-180, 180)（口径 v2 修正）；
- 水平误差：球面 haversine；沿航向/横向误差按实飞地面航向分解；
- 垂直误差：统一米，`actual − predicted`（正=预测偏低）；
- 每个时域要么给出误差，要么给出结构化排除原因。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

PREDICTION_METRIC_VERSION = "scat-prediction-metrics-v2"
HORIZONS_S: tuple[int, ...] = (60, 180, 300, 600, 900, 1200)
FLIGHT_LEVEL_TO_METRES = 30.48
EARTH_RADIUS_M = 6371008.8
DEFAULT_MAX_TRACK_GAP_S = 300.0
DEFAULT_HEADING_SPAN_S = 30.0
VALID_FLIGHT_LEVEL_UNITS = frozenset({"F"})


class ExclusionReason(StrEnum):
    """样本被排除的结构化原因（写入指标记录）。"""

    OUT_OF_RANGE = "out_of_range"
    HORIZON_NOT_COVERED = "horizon_not_covered"
    INSUFFICIENT_POINTS = "insufficient_points"
    MISSING_TIME = "missing_time"
    MISSING_POSITION = "missing_position"
    MISSING_ALTITUDE = "missing_altitude"
    INVALID_UNIT = "invalid_unit"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """实飞轨迹点（高度单位：米）。"""

    time: datetime
    lat: float
    lon: float
    altitude_m: float | None = None


@dataclass(frozen=True, slots=True)
class PredictedPoint:
    """预测航路点（高度单位：米）。"""

    time: datetime
    lat: float
    lon: float
    altitude_m: float | None = None


@dataclass(frozen=True, slots=True)
class SnapshotComparison:
    """一个快照在一个预测时域上的对齐结果。"""

    snapshot_time: datetime
    horizon_s: float
    target_time: datetime
    predicted: PredictedPoint | None
    actual: TrackPoint | None
    horizontal_error_m: float | None
    vertical_error_m: float | None
    along_track_m: float | None
    cross_track_m: float | None
    valid: bool
    exclusion_reason: ExclusionReason | None

    @property
    def key(self) -> tuple[datetime, float]:
        return (self.snapshot_time, self.horizon_s)


def parse_iso_utc(value: Any) -> datetime | None:
    """解析 SCAT 的无时区 ISO 文本/ datetime；无时区按 UTC。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """球面 haversine 距离（米）；经度差取最短弧（跨 ±180°）。"""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(_shortest_lon_delta(lon2 - lon1))
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(max(a, 0.0), 1.0)))


def local_offset_metres(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> tuple[float, float]:
    """以点 1 为原点的局部东/北偏移（米），用于沿航向/横向分解。"""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(_shortest_lon_delta(lon2 - lon1))
    north = EARTH_RADIUS_M * dlat
    east = EARTH_RADIUS_M * math.cos(math.radians((lat1 + lat2) / 2.0)) * dlon
    return east, north


def decompose_along_cross(
    *,
    east_m: float,
    north_m: float,
    heading_deg: float,
) -> tuple[float, float]:
    """按地面航向（北为 0、顺时针为正）分解为沿航向/横向分量（正=右偏）。"""
    heading = math.radians(heading_deg)
    along = north_m * math.cos(heading) + east_m * math.sin(heading)
    cross = east_m * math.cos(heading) - north_m * math.sin(heading)
    return along, cross


def _shortest_lon_delta(delta: float) -> float:
    return (delta + 180.0) % 360.0 - 180.0


def build_track(
    points: Iterable[dict[str, Any]],
    *,
    time_field: str = "timestamp",
    lat_field: str = "lat",
    lon_field: str = "lon",
    altitude_field: str = "altitude_m",
) -> tuple[list[TrackPoint], int]:
    """实飞轨迹点 → 按时间排序的 TrackPoint 列表；返回 (轨迹, 重复时间戳数)。

    无效时间/位置的点直接跳过；同一时间戳只保留最后出现的点（重复计数单独返回）。
    """
    parsed: list[TrackPoint] = []
    for raw in points:
        time = parse_iso_utc(raw.get(time_field))
        lat = _as_float(raw.get(lat_field))
        lon = _as_float(raw.get(lon_field))
        if time is None or lat is None or lon is None:
            continue
        parsed.append(
            TrackPoint(
                time=time,
                lat=lat,
                lon=lon,
                altitude_m=_as_float(raw.get(altitude_field)),
            )
        )
    parsed.sort(key=lambda item: item.time)
    deduped: list[TrackPoint] = []
    duplicates = 0
    for track_point in parsed:
        if deduped and deduped[-1].time == track_point.time:
            deduped[-1] = track_point
            duplicates += 1
            continue
        deduped.append(track_point)
    return deduped, duplicates


def build_route(
    route: Iterable[dict[str, Any]],
) -> tuple[list[PredictedPoint], ExclusionReason | None]:
    """预测航路点 → 按时间排序的 PredictedPoint 列表。

    高度单位只接受 `afl_unit = "F"`（flight level，×30.48 → 米）；
    其他单位返回 `invalid_unit`，由调用方决定整快照是否可用。
    """
    parsed: list[PredictedPoint] = []
    for point in route:
        unit = point.get("afl_unit")
        if unit is not None and str(unit).upper() not in VALID_FLIGHT_LEVEL_UNITS:
            return [], ExclusionReason.INVALID_UNIT
        time = parse_iso_utc(point.get("eto"))
        lat = _as_float(point.get("lat"))
        lon = _as_float(point.get("lon"))
        if time is None or lat is None or lon is None:
            continue
        flight_level = _as_float(point.get("afl_value"))
        parsed.append(
            PredictedPoint(
                time=time,
                lat=lat,
                lon=lon,
                altitude_m=None if flight_level is None else flight_level * FLIGHT_LEVEL_TO_METRES,
            )
        )
    parsed.sort(key=lambda item: item.time)
    return parsed, None


def interpolate_track(
    track: list[TrackPoint],
    target: datetime,
    *,
    max_gap_s: float = DEFAULT_MAX_TRACK_GAP_S,
) -> tuple[TrackPoint | None, ExclusionReason | None]:
    """把实飞轨迹线性插值到 target 时刻。"""
    index = bisect_left([point.time for point in track], target) if track else 0
    if not track:
        return None, ExclusionReason.INSUFFICIENT_POINTS
    if index == 0:
        return (track[0], None) if track[0].time == target else (None, ExclusionReason.OUT_OF_RANGE)
    if index >= len(track):
        last = track[-1]
        return (last, None) if last.time == target else (None, ExclusionReason.OUT_OF_RANGE)
    before, after = track[index - 1], track[index]
    if after.time == target:
        return after, None
    if before.time == target:
        return before, None
    gap = (after.time - before.time).total_seconds()
    if gap <= 0 or gap > max_gap_s:
        return None, ExclusionReason.INSUFFICIENT_POINTS
    ratio = (target - before.time).total_seconds() / gap
    return (
        TrackPoint(
            time=target,
            lat=_lerp(before.lat, after.lat, ratio),
            lon=_lerp_lon(before.lon, after.lon, ratio),
            altitude_m=_lerp_optional(before.altitude_m, after.altitude_m, ratio),
        ),
        None,
    )


def interpolate_route(
    route: list[PredictedPoint],
    target: datetime,
) -> tuple[PredictedPoint | None, ExclusionReason | None]:
    """把预测轨迹线性插值到 target 时刻（只使用不早于快照时刻的点，由调用方过滤）。"""
    if not route:
        return None, ExclusionReason.HORIZON_NOT_COVERED
    if target > route[-1].time:
        return None, ExclusionReason.HORIZON_NOT_COVERED
    if target < route[0].time:
        return None, ExclusionReason.OUT_OF_RANGE
    index = bisect_left([point.time for point in route], target)
    if index == 0:
        return route[0], None
    before, after = route[index - 1], route[index]
    if after.time == target:
        return after, None
    gap = (after.time - before.time).total_seconds()
    if gap <= 0:
        return before, None
    ratio = (target - before.time).total_seconds() / gap
    return (
        PredictedPoint(
            time=target,
            lat=_lerp(before.lat, after.lat, ratio),
            lon=_lerp_lon(before.lon, after.lon, ratio),
            altitude_m=_lerp_optional(before.altitude_m, after.altitude_m, ratio),
        ),
        None,
    )


def _lerp(start: float, end: float, ratio: float) -> float:
    return start + (end - start) * ratio


def _lerp_optional(start: float | None, end: float | None, ratio: float) -> float | None:
    if start is None or end is None:
        return start if end is None else end
    return _lerp(start, end, ratio)


def _lerp_lon(start: float, end: float, ratio: float) -> float:
    """经度线性插值：沿最短弧推进后折回 [-180, 180)。

    v1 曾把最短弧增量（度）当作插值比例再交给 ``_lerp``，等于经差被乘了两次：
    实飞点间隔仅数秒时误差不足 1e-4 度、肉眼不可见，但预测航路点间隔可达数十分钟，
    十几度的经差会被放大成二十几度的假位置（例：航班 107828 快照
    ``2016-10-19T13:54:24.799`` + 300 s；详见 docs/PREDICTION_EVALUATION.md 口径 v2）。
    """
    return _wrap_lon(start + _shortest_lon_delta(end - start) * ratio)


def _wrap_lon(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class FlightEvaluation:
    """一个航班的全部比较结果与排除统计。"""

    comparisons: list[SnapshotComparison]
    snapshot_count: int
    duplicate_timestamps: int
    exclusions: dict[str, int]

    @property
    def valid_count(self) -> int:
        return sum(1 for item in self.comparisons if item.valid)


def heading_deg(
    track: list[TrackPoint],
    target: datetime,
    *,
    span_s: float = DEFAULT_HEADING_SPAN_S,
) -> float | None:
    """target 时刻的实飞地面航向（北 0、顺时针为正）；点位不足或跨度超限返回 None。"""
    if len(track) < 2:
        return None
    index = bisect_left([point.time for point in track], target)
    before = track[max(index - 1, 0)]
    after = track[min(index, len(track) - 1)]
    if before.time == after.time:
        return None
    if (after.time - before.time).total_seconds() > span_s:
        return None
    east, north = local_offset_metres(before.lat, before.lon, after.lat, after.lon)
    if east == 0.0 and north == 0.0:
        return None
    return (math.degrees(math.atan2(east, north)) + 360.0) % 360.0


def evaluate_snapshot(
    *,
    snapshot_time: datetime,
    route: list[PredictedPoint],
    track: list[TrackPoint],
    horizons: Iterable[float] = HORIZONS_S,
    max_gap_s: float = DEFAULT_MAX_TRACK_GAP_S,
    heading_span_s: float = DEFAULT_HEADING_SPAN_S,
    route_reason: ExclusionReason | None = None,
) -> list[SnapshotComparison]:
    """按冻结口径把一个快照对齐到实飞轨迹，每个时域输出一条比较记录。

    预测侧只使用 `eto ≥ snapshot_time` 的点（预测轨迹含历史位置，见口径 §1.2）。
    """
    usable_route = [point for point in route if point.time >= snapshot_time]
    comparisons: list[SnapshotComparison] = []
    for horizon in horizons:
        target = snapshot_time + timedelta(seconds=float(horizon))
        predicted: PredictedPoint | None
        predicted_reason: ExclusionReason | None
        if route_reason is not None:
            predicted, predicted_reason = None, route_reason
        else:
            predicted, predicted_reason = interpolate_route(usable_route, target)
        if predicted is None or predicted_reason is not None:
            comparisons.append(
                _excluded(
                    snapshot_time,
                    horizon,
                    target,
                    predicted,
                    None,
                    predicted_reason or ExclusionReason.HORIZON_NOT_COVERED,
                )
            )
            continue
        actual, actual_reason = interpolate_track(track, target, max_gap_s=max_gap_s)
        if actual is None or actual_reason is not None:
            comparisons.append(
                _excluded(
                    snapshot_time,
                    horizon,
                    target,
                    predicted,
                    actual,
                    actual_reason or ExclusionReason.INSUFFICIENT_POINTS,
                )
            )
            continue
        using_missing_altitude = actual.altitude_m is None or predicted.altitude_m is None
        along: float | None = None
        cross: float | None = None
        heading = heading_deg(track, target, span_s=heading_span_s)
        if heading is not None:
            east, north = local_offset_metres(actual.lat, actual.lon, predicted.lat, predicted.lon)
            along, cross = decompose_along_cross(east_m=east, north_m=north, heading_deg=heading)
        comparisons.append(
            SnapshotComparison(
                snapshot_time=snapshot_time,
                horizon_s=float(horizon),
                target_time=target,
                predicted=predicted,
                actual=actual,
                horizontal_error_m=haversine_metres(
                    predicted.lat,
                    predicted.lon,
                    actual.lat,
                    actual.lon,
                ),
                vertical_error_m=(
                    None
                    if using_missing_altitude
                    else float(actual.altitude_m) - float(predicted.altitude_m)  # type: ignore[arg-type]
                ),
                along_track_m=along,
                cross_track_m=cross,
                valid=True,
                exclusion_reason=None,
            )
        )
    return comparisons


def _excluded(
    snapshot_time: datetime,
    horizon: float,
    target: datetime,
    predicted: PredictedPoint | None,
    actual: TrackPoint | None,
    reason: ExclusionReason,
) -> SnapshotComparison:
    return SnapshotComparison(
        snapshot_time=snapshot_time,
        horizon_s=float(horizon),
        target_time=target,
        predicted=predicted,
        actual=actual,
        horizontal_error_m=None,
        vertical_error_m=None,
        along_track_m=None,
        cross_track_m=None,
        valid=False,
        exclusion_reason=reason,
    )


def evaluate_flight(
    *,
    plots: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    horizons: Iterable[float] = HORIZONS_S,
    max_gap_s: float = DEFAULT_MAX_TRACK_GAP_S,
    heading_span_s: float = DEFAULT_HEADING_SPAN_S,
) -> FlightEvaluation:
    """整航班评估：实飞轨迹建一次，逐个快照对齐并汇总排除原因。

    `plots`/`snapshots` 需已由上层（repository/adapter）映射为简单字段：
    实飞点 `timestamp/lat/lon/altitude_m`（米），快照 `time_stamp/route[]`（`eto/lat/lon/afl_value/afl_unit`）。
    """
    track, duplicates = build_track(plots)
    comparisons: list[SnapshotComparison] = []
    exclusions: dict[str, int] = {}
    snapshot_count = 0
    for snapshot in snapshots:
        snapshot_time = parse_iso_utc(snapshot.get("time_stamp"))
        if snapshot_time is None:
            exclusions[ExclusionReason.MISSING_TIME.value] = (
                exclusions.get(ExclusionReason.MISSING_TIME.value, 0) + 1
            )
            continue
        route, route_reason = build_route(snapshot.get("route") or [])
        snapshot_count += 1
        items = evaluate_snapshot(
            snapshot_time=snapshot_time,
            route=route,
            track=track,
            horizons=horizons,
            max_gap_s=max_gap_s,
            heading_span_s=heading_span_s,
            route_reason=route_reason,
        )
        comparisons.extend(items)
        for item in items:
            if item.exclusion_reason is None:
                continue
            key = item.exclusion_reason.value
            exclusions[key] = exclusions.get(key, 0) + 1
    if duplicates:
        exclusions[ExclusionReason.DUPLICATE_TIMESTAMP.value] = (
            exclusions.get(ExclusionReason.DUPLICATE_TIMESTAMP.value, 0) + duplicates
        )
    return FlightEvaluation(
        comparisons=comparisons,
        snapshot_count=snapshot_count,
        duplicate_timestamps=duplicates,
        exclusions=exclusions,
    )


__all__ = [
    "DEFAULT_HEADING_SPAN_S",
    "DEFAULT_MAX_TRACK_GAP_S",
    "EARTH_RADIUS_M",
    "FLIGHT_LEVEL_TO_METRES",
    "HORIZONS_S",
    "PREDICTION_METRIC_VERSION",
    "VALID_FLIGHT_LEVEL_UNITS",
    "ExclusionReason",
    "FlightEvaluation",
    "PredictedPoint",
    "SnapshotComparison",
    "TrackPoint",
    "build_route",
    "build_track",
    "decompose_along_cross",
    "evaluate_flight",
    "evaluate_snapshot",
    "haversine_metres",
    "heading_deg",
    "interpolate_route",
    "interpolate_track",
    "local_offset_metres",
    "parse_iso_utc",
]
