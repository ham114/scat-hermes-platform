"""SCAT 轨迹阶段划分（论文口径）：三维 RDP 分段 + 六类基础阶段。

方法来源（论文所用脚本，逐条对应）：

- ``SCAT_RDP.py``：把轨迹转成 **ENU 三维坐标**（E/N/U 统一为 km，高度由
  ``measured_flight_level`` × 100 ft × 0.3048 m 再除以 1000 得到），对整条轨迹做
  Ramer–Douglas–Peucker 简化，``epsilon=0.1``（km，即 100 m）；由保留点索引构造
  ``segment_id``（相邻关键点闭区间，边界点归后一段）。
- ``SCAT_PHASES.py``：每段取 ``vert``（I062/200.vert：0 平飞 / 1 上升 / 2 下降）众数
  （并列按 0→1→2），再用点自身的 ``measured_flight_level`` 落六类：
  ``vert=0`` 且 FL ≥ 300 → ``high_cruise``，否则 ``midlow_level``；
  ``vert=1`` 且 FL < 80 → ``takeoff_initial_climb``，否则 ``climb``；
  ``vert=2`` 且 FL < 40 → ``approach``，否则 ``descent``；FL 缺失按 vert 兜底；
  FL 先裁剪到 [0, 450]。
- 论文附录 §SCAT：窗口标签取观测/未来两段众数，不同模式构成 ordered transition label。

注：``SCAT_PHASES.py`` 的 docstring 写 "进场: seg_vert in {0,2} 且 FL<40"，但其**代码**
在 ``seg_vert==0`` 分支一律返回 ``midlow_level``。本模块按**代码**实现（论文实际使用的口径）。

模块为纯计算（不访问数据库），RDP 与 ENU 自行实现，避免新增第三方依赖。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from math import cos, radians, sin, sqrt

PHASE_TAKEOFF_INITIAL_CLIMB = "takeoff_initial_climb"
PHASE_CLIMB = "climb"
PHASE_HIGH_CRUISE = "high_cruise"
PHASE_MIDLOW_LEVEL = "midlow_level"
PHASE_DESCENT = "descent"
PHASE_APPROACH = "approach"
PHASE_UNKNOWN = "unknown"

# 众数并列时的稳定优先级顺序。
PHASE_ORDER: tuple[str, ...] = (
    PHASE_TAKEOFF_INITIAL_CLIMB,
    PHASE_CLIMB,
    PHASE_HIGH_CRUISE,
    PHASE_MIDLOW_LEVEL,
    PHASE_DESCENT,
    PHASE_APPROACH,
    PHASE_UNKNOWN,
)

# SCAT_RDP.py：epsilon=0.1（E/N/U 单位 km）。
RDP_TOLERANCE_KM = 0.1
# SCAT_PHASES.py：段内 vert 众数并列时优先 0（平飞）→1（上升）→2（下降）。
VERT_TIE_BREAK: tuple[int, ...] = (0, 1, 2)
FL_MIN = 0.0
FL_MAX = 450.0
TAKEOFF_MAX_FL = 80.0
HIGH_CRUISE_MIN_FL = 300.0
APPROACH_MAX_FL = 40.0
FL_TO_KM = 100.0 * 0.3048 / 1000.0  # 1 FL = 100 ft = 30.48 m = 0.03048 km

# WGS84 椭球参数（CAL_ENU.py 同口径）。
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

# 航班级特征把六类归并为爬升/巡航/下降三桶（与 FlightFeaturesRecord 字段对应）。
PHASE_TO_FEATURE_BUCKET: dict[str, str] = {
    PHASE_TAKEOFF_INITIAL_CLIMB: "climb",
    PHASE_CLIMB: "climb",
    PHASE_HIGH_CRUISE: "cruise",
    PHASE_MIDLOW_LEVEL: "cruise",
    PHASE_DESCENT: "descent",
    PHASE_APPROACH: "descent",
    PHASE_UNKNOWN: "cruise",
}


@dataclass(frozen=True, slots=True)
class PhasePoint:
    """一个轨迹点的阶段标注结果。"""

    time: datetime | None
    lat: float
    lon: float
    flight_level: float | None
    vertical_mode: int | None
    segment_index: int
    phase: str


@dataclass(frozen=True, slots=True)
class PhaseSegment:
    """一个 RDP 段的汇总（供统计与可视化使用）。"""

    index: int
    start_time: datetime | None
    end_time: datetime | None
    point_count: int
    mean_flight_level: float | None
    vertical_mode: int
    phase: str
    duration_s: float | None


def flight_level_to_km(flight_level: float) -> float:
    """FL → 高度（km），与 ``load_trajectory_from_csv`` 的换算一致。"""
    return flight_level * FL_TO_KM


def enu_from_geodetic(
    points: Sequence[tuple[float, float, float | None]],
) -> list[tuple[float, float, float]]:
    """(lat, lon, 高度[km]) → 局部 ENU（km），原点取第一个点，ECEF 换算同 CAL_ENU.py。"""
    if not points:
        return []
    origin_lat, origin_lon, origin_h = points[0]
    x0, y0, z0 = _geodetic_to_ecef(origin_lat, origin_lon, origin_h or 0.0)
    lat0 = radians(origin_lat)
    lon0 = radians(origin_lon)
    sin_lat0, cos_lat0 = sin(lat0), cos(lat0)
    sin_lon0, cos_lon0 = sin(lon0), cos(lon0)

    coordinates: list[tuple[float, float, float]] = []
    for lat, lon, height in points:
        x, y, z = _geodetic_to_ecef(lat, lon, height or 0.0)
        dx, dy, dz = x - x0, y - y0, z - z0
        east = -sin_lon0 * dx + cos_lon0 * dy
        north = -sin_lat0 * cos_lon0 * dx - sin_lat0 * sin_lon0 * dy + cos_lat0 * dz
        up = cos_lat0 * cos_lon0 * dx + cos_lat0 * sin_lon0 * dy + sin_lat0 * dz
        coordinates.append((east / 1000.0, north / 1000.0, up / 1000.0))
    return coordinates


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, height_km: float) -> tuple[float, float, float]:
    lat = radians(lat_deg)
    lon = radians(lon_deg)
    height_m = height_km * 1000.0
    sin_lat, cos_lat = sin(lat), cos(lat)
    sin_lon, cos_lon = sin(lon), cos(lon)
    prime_vertical = WGS84_A / sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    x = (prime_vertical + height_m) * cos_lat * cos_lon
    y = (prime_vertical + height_m) * cos_lat * sin_lon
    z = (prime_vertical * (1 - WGS84_E2) + height_m) * sin_lat
    return x, y, z  # ECEF（m）；由调用方换算为 ENU km


def rdp_keep_mask(
    coordinates: Sequence[Sequence[float]],
    *,
    tolerance: float = RDP_TOLERANCE_KM,
) -> list[bool]:
    """N 维 Ramer–Douglas–Peucker：返回长度与输入相同的保留点掩码。

    距离为点到首尾弦线的欧氏垂距（与 ``rdp`` 包的 3D 语义一致）；迭代实现，无递归深度风险。
    """
    count = len(coordinates)
    if count == 0:
        return []
    mask = [False] * count
    mask[0] = True
    mask[count - 1] = True
    if count <= 2:
        return mask

    stack: list[tuple[int, int]] = [(0, count - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        farthest, distance = _farthest_point(coordinates, start, end)
        if distance <= tolerance:
            continue
        mask[farthest] = True
        stack.append((start, farthest))
        stack.append((farthest, end))
    return mask


def _farthest_point(
    coordinates: Sequence[Sequence[float]],
    start: int,
    end: int,
) -> tuple[int, float]:
    anchor = coordinates[start]
    target = coordinates[end]
    direction = [target[axis] - anchor[axis] for axis in range(len(anchor))]
    length_sq = sum(component * component for component in direction)
    farthest = start
    best = -1.0
    for index in range(start + 1, end):
        point = coordinates[index]
        if length_sq == 0.0:
            distance_sq = sum(
                (point[axis] - anchor[axis]) ** 2 for axis in range(len(anchor))
            )
        else:
            projection = sum(
                (point[axis] - anchor[axis]) * direction[axis] for axis in range(len(anchor))
            ) / length_sq
            distance_sq = sum(
                (point[axis] - anchor[axis] - projection * direction[axis]) ** 2
                for axis in range(len(anchor))
            )
        if distance_sq > best:
            best = distance_sq
            farthest = index
    return farthest, sqrt(max(best, 0.0))


def segment_ids_from_mask(mask: Sequence[bool]) -> list[int]:
    """保留点掩码 → 每点 segment_id（相邻关键点闭区间，边界点归后一段）。"""
    keep = [index for index, kept in enumerate(mask) if kept]
    if not keep:
        return [0] * len(mask)
    if keep[0] != 0:
        keep.insert(0, 0)
    if keep[-1] != len(mask) - 1:
        keep.append(len(mask) - 1)
    ids = [0] * len(mask)
    for segment_index in range(len(keep) - 1):
        start, end = keep[segment_index], keep[segment_index + 1]
        for position in range(start, end + 1):
            ids[position] = segment_index
    return ids


def rdp_segment_indexes(
    coordinates: Sequence[Sequence[float]],
    *,
    tolerance: float = RDP_TOLERANCE_KM,
) -> list[int]:
    """ENU(km) 轨迹 → 每点段序号（RDP + 相邻关键点闭区间）。"""
    return segment_ids_from_mask(rdp_keep_mask(coordinates, tolerance=tolerance))


def clamp_flight_level(value: float | None) -> float | None:
    if value is None:
        return None
    return min(max(value, FL_MIN), FL_MAX)


def label_phase(flight_level: float | None, vertical_mode: int | None) -> str:
    """按 ``SCAT_PHASES.py::_label_phase`` 给单个点定六类（FL 缺失按 vert 兜底）。"""
    level = clamp_flight_level(flight_level)
    mode = vertical_mode if isinstance(vertical_mode, int) else None
    if level is None:
        return {
            0: PHASE_MIDLOW_LEVEL,
            1: PHASE_CLIMB,
            2: PHASE_DESCENT,
        }.get(mode, PHASE_UNKNOWN)
    if mode == 0:
        return PHASE_HIGH_CRUISE if level >= HIGH_CRUISE_MIN_FL else PHASE_MIDLOW_LEVEL
    if mode == 1:
        return PHASE_TAKEOFF_INITIAL_CLIMB if level < TAKEOFF_MAX_FL else PHASE_CLIMB
    if mode == 2:
        return PHASE_APPROACH if level < APPROACH_MAX_FL else PHASE_DESCENT
    return PHASE_UNKNOWN


def segment_vertical_mode(modes: Iterable[int | None]) -> int:
    """段内 vert 众数；并列时按 VERT_TIE_BREAK；无有效值按平飞（0）。"""
    counts = Counter(int(mode) for mode in modes if isinstance(mode, int))
    if not counts:
        return 0
    top = max(counts.values())
    candidates = {value for value, count in counts.items() if count == top}
    for preferred in VERT_TIE_BREAK:
        if preferred in candidates:
            return preferred
    return min(candidates)


def modal_phase(labels: Iterable[str]) -> str:
    """区间标签 = 众数；并列时按 PHASE_ORDER。"""
    counts = Counter(label for label in labels if label)
    if not counts:
        return PHASE_UNKNOWN
    top = max(counts.values())
    candidates = {value for value, count in counts.items() if count == top}
    for candidate in PHASE_ORDER:
        if candidate in candidates:
            return candidate
    return min(candidates)


def label_trajectory(
    points: Sequence[dict],
    *,
    tolerance_km: float = RDP_TOLERANCE_KM,
    coordinates_km: Sequence[Sequence[float]] | None = None,
    lat_field: str = "lat",
    lon_field: str = "lon",
    flight_level_field: str = "flight_level",
    vertical_mode_field: str = "vertical_mode",
    time_field: str = "time",
) -> tuple[list[PhasePoint], list[PhaseSegment]]:
    """整条轨迹：ENU(km) → 三维 RDP 分段 → 段垂直状态 → 六类标签 → 段汇总。

    ``coordinates_km`` 可直接给出手工构造的 ENU 轨迹（单测/复算用），否则由经纬度与高度换算。
    """
    valid: list[dict] = []
    geodetic: list[tuple[float, float, float | None]] = []
    for point in points:
        lat = point.get(lat_field)
        lon = point.get(lon_field)
        if lat is None or lon is None:
            continue
        valid.append(point)
        level = _as_float(point.get(flight_level_field))
        geodetic.append(
            (float(lat), float(lon), None if level is None else flight_level_to_km(level))
        )
    if not valid:
        return [], []
    coordinates = (
        [tuple(float(value) for value in coordinate) for coordinate in coordinates_km]
        if coordinates_km is not None
        else enu_from_geodetic(geodetic)
    )
    segment_indexes = rdp_segment_indexes(coordinates, tolerance=tolerance_km)

    by_segment: dict[int, list[int]] = {}
    for position, index in enumerate(segment_indexes):
        by_segment.setdefault(index, []).append(position)

    labels: list[str | None] = [None] * len(valid)
    segments: list[PhaseSegment] = []
    for index in sorted(by_segment):
        positions = by_segment[index]
        mode = segment_vertical_mode(
            _as_int(valid[position].get(vertical_mode_field)) for position in positions
        )
        segment_labels: list[str] = []
        for position in positions:
            phase = label_phase(_as_float(valid[position].get(flight_level_field)), mode)
            labels[position] = phase
            segment_labels.append(phase)
        segments.append(
            PhaseSegment(
                index=index,
                start_time=_to_datetime(valid[positions[0]].get(time_field)),
                end_time=_to_datetime(valid[positions[-1]].get(time_field)),
                point_count=len(positions),
                mean_flight_level=_mean(
                    [_as_float(valid[position].get(flight_level_field)) for position in positions]
                ),
                vertical_mode=mode,
                phase=modal_phase(segment_labels),
                duration_s=_duration_s(
                    valid[positions[0]].get(time_field), valid[positions[-1]].get(time_field)
                ),
            )
        )

    labelled = [
        PhasePoint(
            time=_to_datetime(point.get(time_field)),
            lat=float(point[lat_field]),
            lon=float(point[lon_field]),
            flight_level=_as_float(point.get(flight_level_field)),
            vertical_mode=_as_int(point.get(vertical_mode_field)),
            segment_index=segment_index,
            phase=labels[position] or PHASE_UNKNOWN,
        )
        for position, (point, segment_index) in enumerate(zip(valid, segment_indexes))
    ]
    return labelled, segments


def phase_seconds(segments: Iterable[PhaseSegment]) -> dict[str, float]:
    """按六类累计**段时长**（缺失时长的段按 0 计）。

    注意：RDP 关键点会形成单点段（start==end，时长 0），段时长相加会漏掉相邻段之间的
    间隙；航班级“某阶段持续多久”应改用 `interval_phase_seconds`（按点区间归属，总和恒等于轨迹跨度）。
    """
    totals: dict[str, float] = {phase: 0.0 for phase in PHASE_ORDER}
    for segment in segments:
        totals[segment.phase] = totals.get(segment.phase, 0.0) + float(segment.duration_s or 0.0)
    return totals


def interval_phase_seconds(points: Sequence[PhasePoint]) -> dict[str, float]:
    """按点区间累计秒数：区间 [t_i, t_{i+1}) 归入**结束点**所在阶段。

    与标注脚本一致：阶段的时长由“落在该阶段的采样点”决定（脚本用 5 s 定步长计数，
    这里直接用实际时间戳），因此每个区间恰好计一次，总和等于首尾点时间跨度，
    不受单点段/边界点归属影响。时间缺失或乱序的点跳过不计。
    """
    totals: dict[str, float] = {phase: 0.0 for phase in PHASE_ORDER}
    ordered = [point for point in points if point.time is not None]
    for previous, current in pairwise(ordered):
        if current.time is None or previous.time is None:
            continue
        delta = (current.time - previous.time).total_seconds()
        if delta <= 0:
            continue
        totals[current.phase] = totals.get(current.phase, 0.0) + delta
    return totals


def feature_bucket_seconds(seconds: dict[str, float]) -> dict[str, float]:
    """六类秒数 → 航班级三桶（climb/cruise/descent）。"""
    buckets = {"climb": 0.0, "cruise": 0.0, "descent": 0.0}
    for phase, value in seconds.items():
        bucket = PHASE_TO_FEATURE_BUCKET.get(phase)
        if bucket:
            buckets[bucket] += float(value)
    return buckets


def _as_float(value: object) -> float | None:
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


def _as_int(value: object) -> int | None:
    parsed = _as_float(value)
    return None if parsed is None else int(parsed)


def _mean(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return sum(valid) / len(valid) if valid else None


def _to_datetime(value: object) -> datetime | None:
    """SCAT 存无时区 ISO 文本；口径同 `scat.prediction.alignment.parse_iso_utc`。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _duration_s(start: object, end: object) -> float | None:
    first, second = _to_datetime(start), _to_datetime(end)
    if first is None or second is None:
        return None
    return max((second - first).total_seconds(), 0.0)


__all__ = [
    "APPROACH_MAX_FL",
    "FL_MAX",
    "FL_MIN",
    "FL_TO_KM",
    "HIGH_CRUISE_MIN_FL",
    "PHASE_APPROACH",
    "PHASE_CLIMB",
    "PHASE_DESCENT",
    "PHASE_HIGH_CRUISE",
    "PHASE_MIDLOW_LEVEL",
    "PHASE_ORDER",
    "PHASE_TAKEOFF_INITIAL_CLIMB",
    "PHASE_TO_FEATURE_BUCKET",
    "PHASE_UNKNOWN",
    "RDP_TOLERANCE_KM",
    "TAKEOFF_MAX_FL",
    "VERT_TIE_BREAK",
    "PhasePoint",
    "PhaseSegment",
    "clamp_flight_level",
    "enu_from_geodetic",
    "feature_bucket_seconds",
    "flight_level_to_km",
    "interval_phase_seconds",
    "label_phase",
    "label_trajectory",
    "modal_phase",
    "phase_seconds",
    "rdp_keep_mask",
    "rdp_segment_indexes",
    "segment_ids_from_mask",
    "segment_vertical_mode",
]
