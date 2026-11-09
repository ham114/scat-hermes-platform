"""M6 评估协议 v1 测试：截止点采样、误差、排除与聚合。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin

import pytest

from scat.experiments.evaluation import (
    EvaluationConfig,
    SampleResult,
    _percentile,
    aggregate_samples,
    cutoff_times,
    evaluate_track,
)
from scat.prediction import (
    ConstantVelocityHeadingPredictor,
    PredictionRequest,
    PredictionResult,
    TrackPoint,
)
from scat.prediction.alignment import EARTH_RADIUS_M, PredictedPoint, interpolate_track

T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)
HORIZONS = (60.0, 180.0, 300.0)


def _advance(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
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


def _track(*, count: int = 120, step_s: float = 10.0, speed_mps: float = 120.0) -> list[TrackPoint]:
    points: list[TrackPoint] = []
    for index in range(count):
        lat, lon = _advance(59.0, 18.0, 90.0, speed_mps * step_s * index)
        points.append(
            TrackPoint(
                time=T0 + timedelta(seconds=step_s * index),
                lat=lat,
                lon=lon,
                altitude_m=10000.0,
            )
        )
    return points


class _PerfectPredictor:
    """用实飞插值真值作为预测（验证协议管道；不用于评估模型质量）。"""

    predictor_id = "perfect"
    predictor_version = "0"

    def __init__(self, track: list[TrackPoint]) -> None:
        self._track = track

    def predict(self, request: PredictionRequest) -> PredictionResult:
        fixes: list[PredictedPoint] = []
        for target in request.target_times:
            point, _reason = interpolate_track(self._track, target)
            assert point is not None
            fixes.append(point)
        return PredictionResult(self.predictor_id, self.predictor_version, tuple(fixes))


class _RejectingPredictor:
    predictor_id = "rejecting"
    predictor_version = "0"

    def predict(self, request: PredictionRequest) -> PredictionResult:
        raise ValueError("insufficient observation")


def test_percentile_uses_linear_interpolation() -> None:
    values = [float(value) for value in range(1, 11)]

    assert _percentile(values, 0.0) == 1.0
    assert _percentile(values, 0.5) == 5.5
    assert _percentile(values, 0.9) == pytest.approx(9.1)
    assert _percentile([7.0], 0.95) == 7.0


def test_cutoff_sampling_respects_stride_min_observed_and_cap() -> None:
    track = _track(count=120)  # 0..1190 s
    config = EvaluationConfig(
        batch="b",
        predictor_id="p",
        horizons_s=HORIZONS,
        stride_s=120.0,
        max_cutoffs_per_flight=100,
        min_observed_s=60.0,
    )

    cutoffs = cutoff_times(track, config)

    assert [int((cutoff - T0).total_seconds()) for cutoff in cutoffs] == [
        60,
        180,
        300,
        420,
        540,
        660,
        780,
        900,
        1020,
        1140,
    ]

    capped = cutoff_times(
        track,
        EvaluationConfig(
            batch="b",
            predictor_id="p",
            horizons_s=HORIZONS,
            stride_s=60.0,
            max_cutoffs_per_flight=3,
            min_observed_s=60.0,
        ),
    )
    assert len(capped) == 3
    assert capped[0] == T0 + timedelta(seconds=60)


def test_cutoff_sampling_returns_empty_without_future_segment() -> None:
    short = _track(count=5)  # 0..40 s
    config = EvaluationConfig(
        batch="b",
        predictor_id="p",
        horizons_s=HORIZONS,
        min_observed_s=60.0,
    )
    assert cutoff_times(short, config) == []
    assert cutoff_times(_track(count=2), config) == []


def test_evaluate_track_perfect_predictor_yields_zero_error_and_counts_coverage() -> None:
    track = _track(count=120)
    config = EvaluationConfig(
        batch="b",
        predictor_id="p",
        horizons_s=HORIZONS,
        stride_s=300.0,
        max_cutoffs_per_flight=3,
        min_observed_s=60.0,
    )
    cutoffs = cutoff_times(track, config)
    predictor = _PerfectPredictor(track)

    samples, exclusions = evaluate_track(track, predictor, config, external_id="f1")

    assert cutoffs, "sanity: 需要至少一个截止点"
    assert all(sample.error_m == 0.0 for sample in samples)
    covered = sum(
        1
        for cutoff in cutoffs
        for horizon in HORIZONS
        if cutoff + timedelta(seconds=horizon) <= track[-1].time
    )
    assert len(samples) == covered
    assert exclusions.get("out_of_range", 0) == len(cutoffs) * len(HORIZONS) - covered


def test_evaluate_track_counts_predictor_rejections() -> None:
    track = _track(count=60)
    config = EvaluationConfig(
        batch="b",
        predictor_id="p",
        horizons_s=HORIZONS,
        stride_s=120.0,
        max_cutoffs_per_flight=2,
        min_observed_s=60.0,
    )

    samples, exclusions = evaluate_track(track, _RejectingPredictor(), config, external_id="f1")

    assert samples == []
    assert exclusions["predictor_rejected"] == len(cutoff_times(track, config))


def test_aggregate_samples_metrics_and_worst() -> None:
    samples = [
        SampleResult("a", T0, 60.0, 100.0),
        SampleResult("b", T0, 60.0, 200.0),
        SampleResult("c", T0, 60.0, 300.0),
        SampleResult("d", T0, 60.0, 400.0),
        SampleResult("a", T0, 180.0, 50.0),
        SampleResult("b", T0, 180.0, 150.0),
    ]

    metrics, worst = aggregate_samples(samples, horizons=HORIZONS, worst_limit=2)

    first = metrics[0]
    assert (first.horizon_s, first.sample_count, first.flight_count) == (60.0, 4, 4)
    assert first.mae_m == pytest.approx(250.0)
    assert first.rmse_m == pytest.approx(273.8612787525831)
    assert first.median_m == pytest.approx(250.0)
    assert first.p90_m == pytest.approx(370.0)
    assert first.p95_m == pytest.approx(385.0)

    second = metrics[1]
    assert (second.sample_count, second.mae_m) == (2, pytest.approx(100.0))

    empty = metrics[2]
    assert (empty.sample_count, empty.flight_count, empty.mae_m) == (0, 0, None)

    assert [(item.error_m, item.external_id) for item in worst] == [(400.0, "d"), (300.0, "c")]


def test_evaluation_repeats_identically_on_fixed_input() -> None:
    """测试要求：固定输入下基准实验可重复（无随机源）。"""
    track = _track(count=120)
    config = EvaluationConfig(
        batch="b",
        predictor_id="constant-velocity-heading",
        horizons_s=(60.0, 180.0),
        stride_s=120.0,
        max_cutoffs_per_flight=3,
        min_observed_s=60.0,
    )
    predictor = ConstantVelocityHeadingPredictor()

    first_samples, first_exclusions = evaluate_track(track, predictor, config, external_id="f1")
    second_samples, second_exclusions = evaluate_track(track, predictor, config, external_id="f1")

    assert first_samples
    assert first_samples == second_samples
    assert first_exclusions == second_exclusions
    first_metrics, _first_worst = aggregate_samples(first_samples, horizons=config.horizons_s)
    second_metrics, _second_worst = aggregate_samples(second_samples, horizons=config.horizons_s)
    assert first_metrics == second_metrics
