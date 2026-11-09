"""M5-02/M5-03 对齐与误差单测：手工构造轨迹，不依赖数据库。"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from scat.prediction.alignment import (
    ExclusionReason,
    build_route,
    build_track,
    evaluate_flight,
    evaluate_snapshot,
    haversine_metres,
    heading_deg,
    interpolate_route,
    interpolate_track,
)

T0 = datetime(2016, 10, 20, 12, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat()


def _plot(
    offset_s: float,
    lat: float,
    lon: float,
    altitude: float | None = 3000.0,
) -> dict:
    return {
        "timestamp": _iso(T0 + timedelta(seconds=offset_s)),
        "lat": lat,
        "lon": lon,
        "altitude_m": altitude,
    }


def _route_point(
    offset_s: float,
    lat: float,
    lon: float,
    flight_level: float | None = 100.0,
    unit: str = "F",
) -> dict:
    return {
        "eto": _iso(T0 + timedelta(seconds=offset_s)),
        "lat": lat,
        "lon": lon,
        "afl_value": flight_level,
        "afl_unit": unit,
    }


def _east_track() -> list[dict]:
    """向东平飞：5 s 一点，59°N / 18°E 附近（lon 每 5 s 走 0.001°）。"""
    return [
        _plot(0, 59.0, 17.9990),
        _plot(5, 59.0, 18.0000),
        _plot(10, 59.0, 18.0010),
        _plot(15, 59.0, 18.0020),
    ]


def _snapshot(offset_s: float, route: list[dict]) -> dict:
    return {"time_stamp": _iso(T0 + timedelta(seconds=offset_s)), "route": route}


def test_haversine_and_heading_basics():
    assert haversine_metres(59.0, 18.0, 59.0, 18.01) == pytest.approx(573.0, abs=5.0)

    track, duplicates = build_track(_east_track())

    assert duplicates == 0
    assert heading_deg(track, T0 + timedelta(seconds=5)) == pytest.approx(90.0, abs=5.0)


def test_interpolates_between_track_points():
    track, _ = build_track([_plot(0, 59.0, 18.0), _plot(10, 59.1, 18.0)])

    actual, reason = interpolate_track(track, T0 + timedelta(seconds=5))

    assert reason is None
    assert actual is not None
    assert actual.lat == pytest.approx(59.05)
    assert actual.lon == pytest.approx(18.0)


def test_snapshot_horizontal_and_along_cross_errors():
    track, _ = build_track(_east_track())
    route, reason = build_route(
        [
            _route_point(-300, 58.0, 17.0),  # 历史点：必须被忽略
            _route_point(5, 59.0, 18.010),
        ]
    )

    assert reason is None
    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(5,))[0]

    assert item.valid
    assert item.horizontal_error_m == pytest.approx(573.0, abs=5.0)
    assert item.along_track_m == pytest.approx(573.0, abs=6.0)  # 正 = 预测超前
    assert item.cross_track_m == pytest.approx(0.0, abs=5.0)


def test_vertical_error_uses_actual_minus_predicted():
    track, _ = build_track(
        [
            _plot(0, 59.0, 18.0, altitude=3000.0),
            _plot(10, 59.0, 18.0, altitude=3000.0),
        ]
    )
    route, _ = build_route([_route_point(5, 59.0, 18.0, flight_level=95.0)])

    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(5,))[0]

    assert item.vertical_error_m == pytest.approx(3000.0 - 95.0 * 30.48, abs=0.5)


def test_horizon_not_covered_when_route_ends_early():
    track, _ = build_track(_east_track())
    route, _ = build_route([_route_point(5, 59.0, 18.01)])

    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(600,))[0]

    assert not item.valid
    assert item.exclusion_reason is ExclusionReason.HORIZON_NOT_COVERED


def test_out_of_range_when_track_ends_before_target():
    track, _ = build_track([_plot(0, 59.0, 18.0), _plot(5, 59.0, 18.001)])
    route, _ = build_route([_route_point(0, 59.0, 18.0), _route_point(600, 59.0, 18.05)])

    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(600,))[0]

    assert item.exclusion_reason is ExclusionReason.OUT_OF_RANGE


def test_insufficient_points_for_sparse_track():
    track, _ = build_track([_plot(0, 59.0, 18.0), _plot(1200, 59.0, 18.2)])
    route, _ = build_route([_route_point(0, 59.0, 18.0), _route_point(1200, 59.0, 18.2)])

    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(600,))[0]

    assert item.exclusion_reason is ExclusionReason.INSUFFICIENT_POINTS


def test_missing_altitude_keeps_horizontal_sample():
    track, _ = build_track(
        [_plot(0, 59.0, 18.0, altitude=None), _plot(10, 59.0, 18.0, altitude=None)]
    )
    route, _ = build_route([_route_point(5, 59.0, 18.002, flight_level=None)])

    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(5,))[0]

    assert item.valid
    assert item.vertical_error_m is None
    assert item.horizontal_error_m == pytest.approx(114.0, abs=5.0)


def test_invalid_unit_excludes_every_horizon():
    snapshot = _snapshot(0, [_route_point(5, 59.0, 18.01, unit="M")])

    evaluation = evaluate_flight(plots=_east_track(), snapshots=[snapshot], horizons=(60, 300))

    assert evaluation.valid_count == 0
    assert evaluation.exclusions == {ExclusionReason.INVALID_UNIT.value: 2}


def test_antimeridian_crossing_is_handled():
    track, _ = build_track(
        [
            _plot(0, 10.0, 179.9990),
            _plot(5, 10.0, 180.0000),
            _plot(10, 10.0, -179.9990),
        ]
    )
    route, _ = build_route([_route_point(5, 10.0, -179.9990)])

    item = evaluate_snapshot(snapshot_time=T0, route=route, track=track, horizons=(5,))[0]

    # 跨 ±180°：-179.999 与实飞点 180.000 相距约 111 m，而不是绕地球一周
    assert item.horizontal_error_m is not None
    assert item.horizontal_error_m < 200.0


def test_duplicate_timestamps_are_deduped():
    track, duplicates = build_track(
        [
            _plot(0, 59.0, 18.0),
            _plot(5, 59.0, 18.0005),
            _plot(5, 59.0, 18.0010),
            _plot(10, 59.0, 18.0020),
        ]
    )

    assert duplicates == 1
    times = [point.time for point in track]
    assert len(times) == 3
    assert track[1].lon == pytest.approx(18.0010)


def test_evaluate_flight_reports_snapshot_and_exclusion_counts():
    plots = _east_track()
    snapshots = [
        _snapshot(0, [_route_point(5, 59.0, 18.010), _route_point(1200, 59.0, 18.05)]),
        _snapshot(5, [_route_point(600, 59.0, 18.09)]),
    ]

    evaluation = evaluate_flight(plots=plots, snapshots=snapshots, horizons=(60, 600))

    assert evaluation.snapshot_count == 2
    assert len(evaluation.comparisons) == 4
    assert evaluation.duplicate_timestamps == 0
    # 实飞轨迹只到 T0+15s：两个快照的时域都超出实飞范围；快照二 600 s 还超出预测末点
    assert evaluation.valid_count == 0
    assert evaluation.exclusions[ExclusionReason.OUT_OF_RANGE.value] == 3
    assert evaluation.exclusions[ExclusionReason.HORIZON_NOT_COVERED.value] == 1


def test_longitude_is_not_double_scaled_on_sparse_route_segment():
    """回归（口径 v2）：稀疏航路点段（16.6°E → 0.0°E，间隔 3857 s）不得把经差重复计入。

    取自航班 107828 快照 2016-10-19T13:54:24.799：口径 v1 在 +300 s 给出 37.67°E，
    与该段 lat 插值（59.3749°N）自相矛盾；正确值应落在段内（约 15.33°E）。
    """
    route, reason = build_route(
        [
            _route_point(-5.799, 59.10845609618378, 16.64796217922194),
            _route_point(5.201, 59.12162691013725, 16.602428842034534),
            _route_point(3862.201, 62.43496756484558, -7.463660474919776e-07),
        ]
    )

    assert reason is None
    predicted, reason = interpolate_route(route[1:], T0 + timedelta(seconds=300))

    assert reason is None
    assert predicted is not None
    start, end = route[1], route[2]
    ratio = 294.799 / 3857.0
    assert predicted.lat == pytest.approx(start.lat + (end.lat - start.lat) * ratio, abs=1e-9)
    assert end.lon <= predicted.lon <= start.lon  # v1：37.67°E 跑到段外
    assert predicted.lon == pytest.approx(15.33347, abs=1e-3)


def test_antimeridian_sparse_route_segment_uses_shortest_arc():
    route, _ = build_route([_route_point(0, 10.0, 179.5), _route_point(600, 10.0, -179.5)])

    predicted, reason = interpolate_route(route, T0 + timedelta(seconds=300))

    assert reason is None
    assert predicted is not None
    # 中点应落在 ±180°：v1 把 1° 的经差重复计入，得到 0°
    assert abs(abs(predicted.lon) - 180.0) < 0.01


def test_longitude_is_not_double_scaled_when_interpolating_track():
    track, _ = build_track([_plot(0, 10.0, 16.0), _plot(200, 10.0, 0.0)])

    actual, reason = interpolate_track(track, T0 + timedelta(seconds=100))

    assert reason is None
    assert actual is not None
    assert actual.lon == pytest.approx(8.0, abs=1e-6)  # v1 得到 144°


def _lon_on_shortest_arc(lon: float, lon1: float, lon2: float) -> bool:
    """lon 是否落在 lon1→lon2 的最短弧区间内（含端点）。"""
    delta = (lon2 - lon1 + 180.0) % 360.0 - 180.0
    travelled = (lon - lon1 + 180.0) % 360.0 - 180.0
    if delta == 0.0:
        return abs(travelled) < 1e-9
    return -1e-9 <= travelled / delta <= 1.0 + 1e-9


@pytest.mark.parametrize("seed", [7, 23, 97])
def test_route_interpolation_invariants_on_random_segments(seed: int) -> None:
    """不变量（防“带单位的量被当成比例用”一类缺陷）：落在段内、单调推进、端点闭合。

    口径 v1 的经差重复计入会直接破坏这三条：例如 30° 经差在 ratio=0.5 时给出段外位置，
    到起点的球面距离也会超过段长。
    """
    rng = random.Random(seed)
    for _ in range(120):
        lat1 = rng.uniform(-70.0, 70.0)
        lon1 = rng.uniform(-179.0, 179.0)
        lat2 = max(-88.0, min(88.0, lat1 + rng.uniform(-5.0, 5.0)))
        lon2 = ((lon1 + rng.uniform(-30.0, 30.0) + 180.0) % 360.0) - 180.0
        route, reason = build_route([_route_point(0, lat1, lon1), _route_point(600, lat2, lon2)])

        assert reason is None
        span = haversine_metres(lat1, lon1, lat2, lon2)
        travelled_before = -1.0
        for offset in (60, 180, 300, 420):
            point, reason = interpolate_route(route, T0 + timedelta(seconds=offset))

            assert reason is None
            assert point is not None
            assert min(lat1, lat2) - 1e-9 <= point.lat <= max(lat1, lat2) + 1e-9
            assert _lon_on_shortest_arc(point.lon, lon1, lon2)
            travelled = haversine_metres(lat1, lon1, point.lat, point.lon)
            assert travelled <= span + 5.0
            assert travelled > travelled_before - 1e-6
            travelled_before = travelled

        end_point, reason = interpolate_route(route, T0 + timedelta(seconds=600))

        assert reason is None
        assert end_point is not None
        assert end_point.lat == pytest.approx(lat2, abs=1e-9)
        assert end_point.lon == pytest.approx(lon2, abs=1e-9)


@pytest.mark.parametrize("seed", [11, 41])
def test_track_interpolation_invariants_on_random_segments(seed: int) -> None:
    """实飞侧同一组不变量，含跨 ±180°、经差最大 45° 的稀疏段（间隔仍在 300 s 护栏内）。"""
    rng = random.Random(seed)
    for _ in range(120):
        lat1 = rng.uniform(-70.0, 70.0)
        lon1 = rng.uniform(-179.0, 179.0)
        lat2 = max(-88.0, min(88.0, lat1 + rng.uniform(-2.0, 2.0)))
        lon2 = ((lon1 + rng.uniform(-45.0, 45.0) + 180.0) % 360.0) - 180.0
        track, _ = build_track([_plot(0, lat1, lon1), _plot(200, lat2, lon2)])

        point, reason = interpolate_track(track, T0 + timedelta(seconds=100))

        assert reason is None
        assert point is not None
        assert min(lat1, lat2) - 1e-9 <= point.lat <= max(lat1, lat2) + 1e-9
        assert _lon_on_shortest_arc(point.lon, lon1, lon2)
