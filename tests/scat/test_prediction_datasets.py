"""M6-03 数据集划分测试：无航班级泄漏、确定性与边界校验。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scat.prediction import split_flights_by_flight, split_flights_by_time

IDS = [str(100000 + index) for index in range(40)]


def test_split_by_flight_has_no_leakage_and_is_deterministic() -> None:
    first = split_flights_by_flight(IDS, seed=7)
    second = split_flights_by_flight(IDS, seed=7)

    assert first == second
    assert not set(first.train) & set(first.validation)
    assert not set(first.train) & set(first.test)
    assert not set(first.validation) & set(first.test)
    assert first.all_flights == tuple(sorted(IDS))

    assert len(first.train) == 28  # 40 * 0.7
    assert len(first.validation) == 6  # 40 * 0.15
    assert len(first.test) == 6

    different = split_flights_by_flight(IDS, seed=8)
    assert different != first


def test_split_by_flight_rejects_duplicates_and_tiny_inputs() -> None:
    with pytest.raises(ValueError, match="重复"):
        split_flights_by_flight(["1", "1", "2"], seed=1)
    with pytest.raises(ValueError, match="至少需要 3 个航班"):
        split_flights_by_flight(["1", "2"], seed=1)


def test_split_by_time_respects_boundaries() -> None:
    boundary_a = datetime(2016, 10, 17, tzinfo=UTC)
    boundary_b = datetime(2016, 10, 20, tzinfo=UTC)
    flights = [
        ("a", datetime(2016, 10, 16, 8, tzinfo=UTC)),
        ("b", datetime(2016, 10, 17, 0, 0, tzinfo=UTC)),  # 恰在边界一 → validation
        ("c", datetime(2016, 10, 19, 23, tzinfo=UTC)),
        ("d", datetime(2016, 10, 20, 0, 0, tzinfo=UTC)),  # 恰在边界二 → test
        ("e", datetime(2016, 10, 21, 9, tzinfo=UTC)),
    ]

    split = split_flights_by_time(
        flights,
        train_val_boundary=boundary_a,
        val_test_boundary=boundary_b,
    )

    assert split.train == ("a",)
    assert split.validation == ("b", "c")
    assert split.test == ("d", "e")


def test_split_by_time_validates_inputs() -> None:
    boundary_a = datetime(2016, 10, 17, tzinfo=UTC)
    boundary_b = datetime(2016, 10, 20, tzinfo=UTC)
    with pytest.raises(ValueError, match="边界必须递增"):
        split_flights_by_time([], train_val_boundary=boundary_b, val_test_boundary=boundary_a)

    with pytest.raises(ValueError, match="至少一个子集为空"):
        split_flights_by_time(
            [("a", datetime(2016, 10, 16, tzinfo=UTC))],
            train_val_boundary=boundary_a,
            val_test_boundary=boundary_b,
        )

    with pytest.raises(ValueError, match="重复"):
        split_flights_by_time(
            [
                ("a", datetime(2016, 10, 16, tzinfo=UTC)),
                ("a", datetime(2016, 10, 21, tzinfo=UTC)),
            ],
            train_val_boundary=boundary_a,
            val_test_boundary=boundary_b,
        )
