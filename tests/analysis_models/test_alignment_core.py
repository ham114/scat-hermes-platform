"""共享对齐核（GBAR 口径）单元测试：D-22 的单一实现。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scat_model_runtime.alignment_core import (
    DEFAULT_ALIGNMENT_TOLERANCE_S,
    align_actual_future,
    distance_distribution,
    optional_number,
    trajectory_ade_fde,
)

T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)
FEATURES = ("lat", "lon", "measured_flight_level", "vx", "vy", "rocd")


def _candidate(offset_s: float) -> dict[str, Any]:
    return {
        "timestamp": (T0 + timedelta(seconds=offset_s)).isoformat(),
        "lat": 59.0,
        "lon": 18.0,
        "measured_flight_level": 10000.0,
        "vx": 100.0,
        "vy": 20.0,
        "rocd": 0.0,
    }


def test_exact_matches_build_trajectory_and_expected_rows() -> None:
    aligned, rows = align_actual_future(
        [_candidate(60.0), _candidate(120.0)],
        forecast_origin=T0,
        offsets=[60.0, 120.0],
        feature_names=FEATURES,
    )

    assert aligned == [
        {"time_step_s": 60.0, "lat": 59.0, "lon": 18.0, "altitude": 10000.0},
        {"time_step_s": 120.0, "lat": 59.0, "lon": 18.0, "altitude": 10000.0},
    ]
    assert rows == (
        (59.0, 18.0, 10000.0, 100.0, 20.0, 0.0),
        (59.0, 18.0, 10000.0, 100.0, 20.0, 0.0),
    )


def test_nearest_match_with_tolerance_edge() -> None:
    aligned, rows = align_actual_future(
        [_candidate(63.0)],
        forecast_origin=T0,
        offsets=[60.0, 90.0],
        feature_names=FEATURES,
    )

    assert len(aligned) == 1  # 60s 最近差 3s 命中；90s 最近差 27s 超容差
    assert rows[0][0] == 59.0
    assert rows[1] == (None,) * len(FEATURES)

    # 恰好等于容差（6.0s）命中；超过（6.5s）不命中
    _, rows_edge = align_actual_future(
        [_candidate(DEFAULT_ALIGNMENT_TOLERANCE_S)],
        forecast_origin=T0,
        offsets=[0.0],
        feature_names=FEATURES,
    )
    assert rows_edge[0][0] == 59.0

    _, rows_beyond = align_actual_future(
        [_candidate(DEFAULT_ALIGNMENT_TOLERANCE_S + 0.5)],
        forecast_origin=T0,
        offsets=[0.0],
        feature_names=FEATURES,
    )
    assert rows_beyond[0] == (None,) * len(FEATURES)


def test_invalid_candidates_are_skipped_and_values_normalized() -> None:
    candidates: list[Any] = [
        "not-a-dict",
        {"timestamp": "not-a-time"},
        {"timestamp": T0.replace(tzinfo=None).isoformat()},  # 朴素时间丢弃
        {
            "timestamp": (T0 + timedelta(seconds=60)).isoformat(),
            "lat": 59.0,
            "lon": 18.0,
            "measured_flight_level": True,
            "vx": float("nan"),
            "vy": float("inf"),
        },
    ]

    aligned, rows = align_actual_future(
        candidates,
        forecast_origin=T0,
        offsets=[60.0],
        feature_names=FEATURES,
    )

    assert rows[0] == (59.0, 18.0, None, None, None, None)
    assert aligned == [{"time_step_s": 60.0, "lat": 59.0, "lon": 18.0}]


def test_missing_or_non_list_candidates_return_empty_result() -> None:
    assert align_actual_future(
        None,
        forecast_origin=T0,
        offsets=[60.0],
        feature_names=FEATURES,
    ) == ([], ())

    aligned, rows = align_actual_future(
        [],
        forecast_origin=T0,
        offsets=[60.0, 120.0],
        feature_names=FEATURES,
    )
    assert aligned == []
    assert rows == ((None,) * len(FEATURES), (None,) * len(FEATURES))


def test_optional_number_semantics() -> None:
    assert optional_number(3) == 3.0
    assert optional_number(2.5) == 2.5
    assert optional_number(True) is None
    assert optional_number("3") is None
    assert optional_number(float("nan")) is None
    assert optional_number(float("inf")) is None


def test_trajectory_ade_fde_uses_horizontal_haversine_distance() -> None:
    ade, fde = trajectory_ade_fde(
        ((59.0, 18.0), (59.0, 18.01)),
        ((59.0, 18.0), (59.0, 18.0)),
    )

    assert ade == pytest.approx(fde / 2.0)
    assert fde == pytest.approx(573.0, abs=5.0)

    with pytest.raises(ValueError, match="equal length"):
        trajectory_ade_fde(((59.0, 18.0),), ())


def test_distance_distribution_uses_linear_percentiles() -> None:
    mean, median, p90 = distance_distribution((1.0, 10.0))

    assert mean == 5.5
    assert median == 5.5
    assert p90 == pytest.approx(9.1)

    with pytest.raises(ValueError, match="finite and non-negative"):
        distance_distribution((float("nan"),))
