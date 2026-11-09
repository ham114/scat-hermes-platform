"""M6-04/M6-05 基线预测器测试：几何正确性、回退、输入校验与注册表。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin

import pytest

from scat.prediction import (
    PREDICTOR_SCHEMA_VERSION,
    ConstantVelocityHeadingPredictor,
    KalmanConstantVelocityPredictor,
    PredictionRequest,
    PredictorRegistry,
    TrackPoint,
    build_default_registry,
    default_registry,
)
from scat.prediction.alignment import EARTH_RADIUS_M, haversine_metres
from scat.prediction.geo import local_to_geodetic_m

T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)
HORIZONS = (60.0, 180.0, 300.0, 600.0, 900.0, 1200.0)
SPEED_MPS = 120.0
STEP_S = 10.0
COUNT = 30
LAT0, LON0, ALT0 = 59.0, 18.0, 10000.0
SPAN_M = SPEED_MPS * STEP_S * (COUNT - 1)  # 观测段总长 34800 m


def _advance(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """测试自带的大圆推进（独立于被测实现书写）。"""
    angular = distance_m / EARTH_RADIUS_M
    lat1, lon1 = radians(lat), radians(lon)
    brng = radians(bearing_deg)
    sin_lat2 = sin(lat1) * cos(angular) + cos(lat1) * sin(angular) * cos(brng)
    lat2 = asin(max(-1.0, min(1.0, sin_lat2)))
    lon2 = lon1 + atan2(
        sin(brng) * sin(angular) * cos(lat1),
        cos(angular) - sin(lat1) * sin_lat2,
    )
    return degrees(lat2), ((degrees(lon2) + 540.0) % 360.0) - 180.0


def _great_circle_observed() -> list[TrackPoint]:
    """59°N 正东、120 m/s 的大圆观测（每 10 s 一点，共 290 s）。"""
    points: list[TrackPoint] = []
    for index in range(COUNT):
        lat, lon = _advance(LAT0, LON0, 90.0, SPEED_MPS * STEP_S * index)
        points.append(
            TrackPoint(
                time=T0 + timedelta(seconds=STEP_S * index),
                lat=lat,
                lon=lon,
                altitude_m=ALT0,
            )
        )
    return points


def _great_circle_truth(horizons: tuple[float, ...]) -> list[tuple[float, float]]:
    """同一大圆继续前进的真值位置。"""
    return [_advance(LAT0, LON0, 90.0, SPAN_M + SPEED_MPS * h) for h in horizons]


def _enu_observed() -> list[TrackPoint]:
    """以 (59,18) 为原点的 ENU 直线（正东 120 m/s）观测。"""
    points: list[TrackPoint] = []
    for index in range(COUNT):
        lat, lon, _ = local_to_geodetic_m(
            SPEED_MPS * STEP_S * index,
            0.0,
            0.0,
            origin_lat=LAT0,
            origin_lon=LON0,
            origin_alt_m=ALT0,
        )
        points.append(
            TrackPoint(
                time=T0 + timedelta(seconds=STEP_S * index),
                lat=lat,
                lon=lon,
                altitude_m=ALT0,
            )
        )
    return points


def _enu_truth(horizons: tuple[float, ...]) -> list[tuple[float, float]]:
    truth: list[tuple[float, float]] = []
    for horizon in horizons:
        lat, lon, _ = local_to_geodetic_m(
            SPAN_M + SPEED_MPS * horizon,
            0.0,
            0.0,
            origin_lat=LAT0,
            origin_lon=LON0,
            origin_alt_m=ALT0,
        )
        truth.append((lat, lon))
    return truth


def _request(observed: list[TrackPoint]) -> PredictionRequest:
    last = observed[-1].time
    return PredictionRequest(
        observed=tuple(observed),
        target_times=tuple(last + timedelta(seconds=h) for h in HORIZONS),
    )


def _errors(
    predictor: ConstantVelocityHeadingPredictor | KalmanConstantVelocityPredictor,
    observed: list[TrackPoint],
    truth: list[tuple[float, float]],
) -> list[float]:
    result = predictor.predict(_request(observed))
    assert len(result.fixes) == len(HORIZONS)
    return [
        haversine_metres(fix.lat, fix.lon, lat, lon) for fix, (lat, lon) in zip(result.fixes, truth)
    ]


def test_cv_predictor_continues_the_observed_great_circle() -> None:
    errors = _errors(
        ConstantVelocityHeadingPredictor(),
        _great_circle_observed(),
        _great_circle_truth(HORIZONS),
    )

    assert max(errors) < 50.0  # 理想大圆等速：应近似精确


def test_kalman_predictor_tracks_constant_velocity_line() -> None:
    errors = _errors(
        KalmanConstantVelocityPredictor(),
        _enu_observed(),
        _enu_truth(HORIZONS),
    )

    assert errors[2] < 200.0  # 300 s：滤波收敛后应保持百米以内
    assert max(errors) < 500.0


def test_cv_window_falls_back_to_full_segment() -> None:
    # 观测跨度仅 60 s（远小于默认 300 s 窗口）时，确定性回退到整段首末。
    observed = _great_circle_observed()[:7]
    predictor = ConstantVelocityHeadingPredictor(window_s=300.0)
    result = predictor.predict(_request(observed))

    assert len(result.fixes) == len(HORIZONS)
    assert result.predictor_id == "constant-velocity-heading"
    assert result.predictor_version == "1"
    lat, lon = _advance(LAT0, LON0, 90.0, SPEED_MPS * 60.0 + SPEED_MPS * 60.0)
    assert haversine_metres(result.fixes[0].lat, result.fixes[0].lon, lat, lon) < 50.0


def test_predictors_reject_invalid_requests() -> None:
    predictor = ConstantVelocityHeadingPredictor()
    single = [TrackPoint(time=T0, lat=LAT0, lon=LON0, altitude_m=ALT0)]
    with pytest.raises(ValueError, match="观测点不足"):
        predictor.predict(
            PredictionRequest(
                observed=tuple(single),
                target_times=(T0 + timedelta(seconds=60),),
            )
        )

    observed = _great_circle_observed()[:5]
    with pytest.raises(ValueError, match="晚于观测末点"):
        predictor.predict(
            PredictionRequest(observed=tuple(observed), target_times=(observed[-1].time,))
        )
    with pytest.raises(ValueError, match="严格升序"):
        predictor.predict(
            PredictionRequest(
                observed=tuple(observed),
                target_times=(
                    observed[-1].time + timedelta(seconds=120),
                    observed[-1].time + timedelta(seconds=60),
                ),
            )
        )


def test_registry_lists_and_protects_defaults() -> None:
    listed = default_registry.list_predictors()
    assert ("constant-velocity-heading", "1") in listed
    assert ("kalman-cv", "1") in listed
    assert default_registry.get("kalman-cv") is not None

    fresh = build_default_registry()
    with pytest.raises(ValueError, match="已注册"):
        fresh.register(ConstantVelocityHeadingPredictor())
    with pytest.raises(KeyError, match="未注册"):
        fresh.get("no-such-predictor")

    registry = PredictorRegistry()
    assert registry.list_predictors() == []
    assert PREDICTOR_SCHEMA_VERSION == 1
