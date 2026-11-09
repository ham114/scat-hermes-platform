"""SCAT 轨迹阶段划分单测：三维 ENU RDP 分段 + 六类标签（论文脚本口径）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scat.preprocessing.phases import (
    PHASE_APPROACH,
    PHASE_CLIMB,
    PHASE_DESCENT,
    PHASE_HIGH_CRUISE,
    PHASE_MIDLOW_LEVEL,
    PHASE_ORDER,
    PHASE_TAKEOFF_INITIAL_CLIMB,
    PHASE_TO_FEATURE_BUCKET,
    PHASE_UNKNOWN,
    RDP_TOLERANCE_KM,
    PhasePoint,
    enu_from_geodetic,
    feature_bucket_seconds,
    flight_level_to_km,
    interval_phase_seconds,
    label_phase,
    label_trajectory,
    modal_phase,
    phase_seconds,
    rdp_segment_indexes,
    segment_ids_from_mask,
    segment_vertical_mode,
)

T0 = datetime(2016, 10, 20, 12, 0, tzinfo=UTC)


def test_flight_level_to_km_matches_script_conversion():
    # SCAT_RDP.load_trajectory_from_csv：FL × 100 ft × 0.3048 m → km
    assert flight_level_to_km(350.0) == pytest.approx(10.668)
    assert flight_level_to_km(0.0) == 0.0
    assert RDP_TOLERANCE_KM == 0.1


def test_enu_from_geodetic_is_local_km():
    coordinates = enu_from_geodetic([(59.0, 18.0, 0.0), (59.0, 18.01, 0.0), (59.01, 18.0, 1.0)])

    assert coordinates[0] == pytest.approx((0.0, 0.0, 0.0))
    east, north, up = coordinates[1]
    assert east == pytest.approx(0.572, rel=0.01)
    assert north == pytest.approx(0.0, abs=1e-3)  # WGS84 曲率带来的厘米级串扰
    assert up == pytest.approx(0.0, abs=1e-3)
    assert coordinates[2][1] == pytest.approx(1.112, rel=0.01)  # 0.01° 纬度
    assert coordinates[2][2] == pytest.approx(1.0, rel=0.01)  # 高度 1 km


def test_rdp_keeps_straight_line_as_single_segment():
    coordinates = [(index * 0.2, 0.0, 10.0) for index in range(20)]

    assert rdp_segment_indexes(coordinates) == [0] * 20


def test_rdp_splits_at_turning_corner():
    # 先向东 12 km，再向北 12 km（ENU km）
    east = [(index * 1.0, 0.0, 10.0) for index in range(13)]
    north = [(12.0, index * 1.0, 10.0) for index in range(1, 13)]

    indexes = rdp_segment_indexes(east + north)

    assert set(indexes) == {0, 1}
    assert indexes[:12] == [0] * 12
    assert indexes[12] == 1  # 拐点归后一段（闭区间、后写覆盖）
    assert indexes[-1] == 1


def test_rdp_ignores_deviation_below_tolerance():
    # 50 m 级抖动 < 0.1 km 容差 → 仍是一段
    coordinates = [(0.0, 0.0, 0.0), (1.0, 0.04, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0)]

    assert rdp_segment_indexes(coordinates) == [0, 0, 0, 0]


def test_rdp_is_three_dimensional():
    # 水平方向始终向东直线；高度先平后爬（末尾 3 km 内升高 2 km）
    # 三维在高度折点处切分，忽略高度的二维投影仍只有一段
    with_climb = [(index * 0.25, 0.0, 10.0) for index in range(8)] + [
        (index * 0.25, 0.0, 10.0 + (index - 7) * 0.5) for index in range(8, 12)
    ]
    horizontal_only = [(east, north) for east, north, _ in with_climb]

    assert set(rdp_segment_indexes(with_climb)) == {0, 1}
    assert rdp_segment_indexes(horizontal_only) == [0] * 12


def test_segment_ids_keep_boundary_in_later_segment():
    assert segment_ids_from_mask([True, False, True, False, True]) == [0, 0, 1, 1, 1]
    # 首尾未保留时自动补齐
    assert segment_ids_from_mask([False, True, False]) == [0, 1, 1]


def test_label_phase_follows_paper_rules():
    # vert=0：FL>=300 高空巡航，其余中低空（含 FL<40，与脚本代码一致）
    assert label_phase(350.0, 0) == PHASE_HIGH_CRUISE
    assert label_phase(300.0, 0) == PHASE_HIGH_CRUISE
    assert label_phase(299.9, 0) == PHASE_MIDLOW_LEVEL
    assert label_phase(30.0, 0) == PHASE_MIDLOW_LEVEL
    # vert=1：FL<80 起飞初始爬升，否则爬升
    assert label_phase(79.9, 1) == PHASE_TAKEOFF_INITIAL_CLIMB
    assert label_phase(80.0, 1) == PHASE_CLIMB
    assert label_phase(400.0, 1) == PHASE_CLIMB
    # vert=2：FL<40 进场，否则下降
    assert label_phase(39.9, 2) == PHASE_APPROACH
    assert label_phase(40.0, 2) == PHASE_DESCENT
    # 缺失高度按 vert 兜底；未知 vert → unknown
    assert label_phase(None, 0) == PHASE_MIDLOW_LEVEL
    assert label_phase(None, 1) == PHASE_CLIMB
    assert label_phase(None, 2) == PHASE_DESCENT
    assert label_phase(100.0, 7) == PHASE_UNKNOWN
    # FL 裁剪到 [0, 450]
    assert label_phase(1000.0, 1) == PHASE_CLIMB
    assert label_phase(-50.0, 1) == PHASE_TAKEOFF_INITIAL_CLIMB


def test_segment_vertical_mode_tie_break_and_empty():
    assert segment_vertical_mode([1, 1, 1, 0, 2]) == 1
    # 并列时按 VERT_TIE_BREAK = (0, 1, 2)
    assert segment_vertical_mode([1, 1, 0, 0]) == 0
    assert segment_vertical_mode([2, 2, 1, 1]) == 1
    assert segment_vertical_mode([]) == 0
    assert segment_vertical_mode([None, None]) == 0


def test_modal_phase_tie_uses_phase_order():
    assert modal_phase([PHASE_CLIMB, PHASE_DESCENT]) == PHASE_CLIMB
    assert modal_phase([PHASE_DESCENT, PHASE_DESCENT, PHASE_APPROACH]) == PHASE_DESCENT
    assert modal_phase([]) == PHASE_UNKNOWN
    assert PHASE_ORDER[0] == PHASE_TAKEOFF_INITIAL_CLIMB


def _flight_point(
    offset_s: float,
    lat: float,
    lon: float,
    flight_level: float | None,
    vertical_mode: int | None,
) -> dict:
    return {
        "time": (T0 + timedelta(seconds=offset_s)).replace(tzinfo=None).isoformat(),
        "lat": lat,
        "lon": lon,
        "flight_level": flight_level,
        "vertical_mode": vertical_mode,
    }


def test_label_trajectory_assigns_six_class_labels_per_segment():
    points: list[dict] = []
    # 段一：低空爬升并转向（vert=1，FL 10→70）→ takeoff_initial_climb
    for index in range(12):
        points.append(
            _flight_point(index * 5, 59.0 + index * 0.01, 18.0 + index * 0.01, 10.0 + index * 5, 1)
        )
    # 段二：高空平飞直行（vert=0，FL 350）→ high_cruise
    for index in range(12):
        points.append(_flight_point(60 + index * 5, 59.11, 18.11 + index * 0.01, 350.0, 0))
    # 段三：下降并转向（vert=2，FL 90→10）→ 下降与进场
    for index in range(12):
        points.append(
            _flight_point(
                120 + index * 5, 59.11 + index * 0.01, 18.22 + index * 0.01, 90.0 - index * 8, 2
            )
        )

    labelled, segments = label_trajectory(points)

    phases = [segment.phase for segment in segments]
    assert PHASE_TAKEOFF_INITIAL_CLIMB in phases
    assert PHASE_HIGH_CRUISE in phases
    assert PHASE_DESCENT in phases
    assert len(labelled) == len(points)
    assert {point.segment_index for point in labelled} == {segment.index for segment in segments}
    modes = {segment.index: segment.vertical_mode for segment in segments}
    assert [point.phase for point in labelled] == [
        label_phase(point.flight_level, modes[point.segment_index]) for point in labelled
    ]
    cruise_segment = next(segment for segment in segments if segment.phase == PHASE_HIGH_CRUISE)
    assert cruise_segment.vertical_mode == 0
    assert cruise_segment.mean_flight_level == pytest.approx(350.0)
    assert cruise_segment.point_count == 11  # 拐点归前一段，巡航段少一个边界点
    assert cruise_segment.duration_s == pytest.approx(50.0)


def test_label_trajectory_accepts_precomputed_enu():
    points = [
        {"time": None, "lat": 0.0, "lon": 0.0, "flight_level": 350.0, "vertical_mode": 0},
        {"time": None, "lat": 0.0, "lon": 0.0, "flight_level": 350.0, "vertical_mode": 0},
    ]
    coordinates = [(0.0, 0.0, 10.0), (3.0, 0.0, 10.5)]

    _, segments = label_trajectory(points, coordinates_km=coordinates)

    assert len(segments) == 1
    assert segments[0].phase == PHASE_HIGH_CRUISE
    assert segments[0].duration_s is None  # 无时间时不做时长推断


def test_phase_seconds_and_feature_buckets():
    points: list[dict] = []
    for index in range(24):
        # 三维直线爬升（水平 0.1 km/步、高度线性）→ 单段，段垂直众数 = 1
        points.append(_flight_point(index * 5, 59.0, 18.0 + index * 0.002, 100.0 + index, 1))
    _, segments = label_trajectory(points)

    seconds = phase_seconds(segments)
    buckets = feature_bucket_seconds(seconds)

    assert len(segments) == 1
    assert seconds[PHASE_CLIMB] == pytest.approx(115.0)
    assert buckets["climb"] == pytest.approx(115.0)
    assert buckets["cruise"] == 0.0
    assert buckets["descent"] == 0.0
    assert PHASE_TO_FEATURE_BUCKET[PHASE_HIGH_CRUISE] == "cruise"
    assert PHASE_TO_FEATURE_BUCKET[PHASE_MIDLOW_LEVEL] == "cruise"
    assert PHASE_TO_FEATURE_BUCKET[PHASE_APPROACH] == "descent"
    assert PHASE_TO_FEATURE_BUCKET[PHASE_TAKEOFF_INITIAL_CLIMB] == "climb"


def test_interval_phase_seconds_attributes_interval_to_ending_point():
    """航班级阶段时长按点区间归属：单点段（RDP 关键点）不再造成缺口。"""

    def point(minute: int, phase: str) -> PhasePoint:
        return PhasePoint(
            time=datetime(2016, 10, 16, 16, minute, tzinfo=UTC),
            lat=59.0,
            lon=18.0,
            flight_level=350.0,
            vertical_mode=0,
            segment_index=0,
            phase=phase,
        )

    seconds = interval_phase_seconds(
        [point(0, PHASE_HIGH_CRUISE), point(1, PHASE_HIGH_CRUISE), point(2, PHASE_DESCENT)]
    )

    assert seconds[PHASE_HIGH_CRUISE] == pytest.approx(60.0)
    assert seconds[PHASE_DESCENT] == pytest.approx(60.0)
    assert sum(seconds.values()) == pytest.approx(120.0)
    # 时间缺失的点不参与累计
    missing = PhasePoint(
        time=None,
        lat=59.0,
        lon=18.0,
        flight_level=350.0,
        vertical_mode=0,
        segment_index=0,
        phase=PHASE_CLIMB,
    )
    assert sum(interval_phase_seconds([point(0, PHASE_CLIMB), missing]).values()) == 0.0
