"""M5-06 指标汇总测试：分组（overall/phase/aircraft_type）、MAE/RMSE 与样本量。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    materialize_prediction_metric_summaries,
)
from scat.prediction.alignment import PREDICTION_METRIC_VERSION

BATCH = "scat20161015_20161021"

# (航班, 时域) → 和/平方和（与 Mongo 聚合输出的字段一致）
PER_FLIGHT_ROWS: list[dict[str, Any]] = [
    {
        "_id": {"external_id": "100001", "horizon_s": 60.0},
        "count": 10,
        "sum_h": 1000.0,
        "sum_h2": 200000.0,
        "sum_abs_v": 100.0,
        "sum_v2": 2000.0,
        "sum_abs_along": 600.0,
        "sum_abs_cross": 800.0,
    },
    {
        "_id": {"external_id": "100002", "horizon_s": 60.0},
        "count": 30,
        "sum_h": 6000.0,
        "sum_h2": 1200000.0,
        "sum_abs_v": 900.0,
        "sum_v2": 27000.0,
        "sum_abs_along": 3000.0,
        "sum_abs_cross": 4800.0,
    },
    {
        "_id": {"external_id": "100001", "horizon_s": 180.0},
        "count": 4,
        "sum_h": 800.0,
        "sum_h2": 160000.0,
        "sum_abs_v": 40.0,
        "sum_v2": 400.0,
        "sum_abs_along": 240.0,
        "sum_abs_cross": 320.0,
    },
]

PHASE_ROWS: list[dict[str, Any]] = [
    {"external_id": "100001", "dominant_phase": "high_cruise"},
    {"external_id": "100002", "dominant_phase": "descent"},
    {"external_id": "100003", "dominant_phase": "descent"},  # 无指标样本
]

FEATURE_ROWS: list[dict[str, Any]] = [
    {"external_id": "100001", "aircraft_type": "B738"},
    {"external_id": "100002", "aircraft_type": "A320"},
]


def _cursor(rows: list[dict[str, Any]]) -> Mock:
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def _repository(
    *,
    upserts: list[tuple[str, Any]] | None = None,
    per_flight: list[dict[str, Any]] | None = None,
) -> MaterializationRepository:
    repository = MaterializationRepository(Mock())
    collections: dict[str, Mock] = {}

    def _collection(kind: str) -> Mock:
        if kind not in collections:
            collection = Mock()
            if kind == "prediction_metrics":
                collection.aggregate = AsyncMock(
                    return_value=_cursor(PER_FLIGHT_ROWS if per_flight is None else per_flight)
                )
            elif kind == "phase_segments":
                collection.aggregate = AsyncMock(return_value=_cursor(PHASE_ROWS))
            elif kind == "flight_features":
                collection.aggregate = AsyncMock(return_value=_cursor(FEATURE_ROWS))
            else:
                collection.aggregate = AsyncMock(return_value=_cursor([]))
            collection.replace_one = AsyncMock(
                side_effect=lambda f, d, **kw: (
                    upserts.append((kind, d)) if upserts is not None else None
                )
            )
            collection.bulk_write = AsyncMock(
                side_effect=lambda operations, **kw: (
                    upserts.extend((kind, operation._doc) for operation in operations)
                    if upserts is not None
                    else None
                )
            )
            collections[kind] = collection
        return collections[kind]

    repository.collection = Mock(side_effect=_collection)  # type: ignore[method-assign]
    return repository


@pytest.mark.asyncio
async def test_summaries_group_by_overall_phase_and_aircraft_type():
    upserts: list[tuple[str, Any]] = []
    repository = _repository(upserts=upserts)

    written = await materialize_prediction_metric_summaries(
        batch=BATCH,
        materialization_repository=repository,
        generated_at=datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
    )

    # 时域 60：overall + 2 个阶段 + 2 个机型 = 5；时域 180：overall + high_cruise + B738 = 3
    assert written == 8
    assert all(kind == "prediction_metric_summaries" for kind, _ in upserts)

    documents = {}
    for _kind, document in upserts:
        documents[(document["group_by"], document["group_value"], document["horizon_s"])] = document

    overall_60 = documents[("overall", "all", 60.0)]
    # MAE = (1000 + 6000) / 40 = 175；RMSE = sqrt((200000 + 1200000) / 40) = sqrt(35000)
    assert overall_60["sample_count"] == 40
    assert overall_60["flight_count"] == 2
    assert overall_60["horizontal_mae_m"] == pytest.approx(175.0)
    assert overall_60["horizontal_rmse_m"] == pytest.approx(35000.0**0.5)
    # 垂直 MAE = (100 + 900) / 40 = 25；RMSE = sqrt((2000 + 27000) / 40)
    assert overall_60["vertical_mae_m"] == pytest.approx(25.0)
    assert overall_60["vertical_rmse_m"] == pytest.approx((29000 / 40) ** 0.5)
    # 沿航向 MAE = (600 + 3000) / 40 = 90；横向 MAE = (800 + 4800) / 40 = 140
    assert overall_60["along_track_mae_m"] == pytest.approx(90.0)
    assert overall_60["cross_track_mae_m"] == pytest.approx(140.0)
    assert overall_60["metric_version"] == PREDICTION_METRIC_VERSION
    assert overall_60["algorithm_version"] == PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION

    descent_60 = documents[("phase", "descent", 60.0)]
    assert descent_60["sample_count"] == 30
    assert descent_60["horizontal_mae_m"] == pytest.approx(200.0)

    # 阶段分组里没有样本的阶段（100003）不产生记录
    assert ("phase", "unknown", 60.0) not in documents


@pytest.mark.asyncio
async def test_summaries_return_zero_without_samples():
    written = await materialize_prediction_metric_summaries(
        batch=BATCH,
        materialization_repository=_repository(per_flight=[]),
    )

    assert written == 0
