"""M5-06 分位数与 metric-summaries CLI 测试：$percentile 接线、降级路径与解析默认值。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization import cli
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    materialize_prediction_metric_summaries,
)

BATCH = "scat20161015_20161021"

PER_FLIGHT_ROWS: list[dict[str, Any]] = [
    {
        "_id": {"external_id": "100001", "horizon_s": 60.0},
        "count": 4,
        "sum_h": 400.0,
        "sum_h2": 80000.0,
        "sum_abs_v": 40.0,
        "sum_v2": 800.0,
        "sum_abs_along": 240.0,
        "sum_abs_cross": 320.0,
    }
]
PERCENTILE_ROWS: list[dict[str, Any]] = [
    {"_id": 60.0, "h": [90.0, 150.0, 170.0], "v": [9.0, 15.0, 17.0]}
]
PHASE_ROWS: list[dict[str, Any]] = [{"external_id": "100001", "dominant_phase": "climb"}]
FEATURE_ROWS: list[dict[str, Any]] = [{"external_id": "100001", "aircraft_type": "B738"}]


def _cursor(rows: list[dict[str, Any]]) -> Mock:
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def _repository(
    *,
    upserts: list[tuple[str, Any]] | None = None,
    percentile_error: bool = False,
) -> MaterializationRepository:
    repository = MaterializationRepository(Mock())
    collections: dict[str, Mock] = {}

    def _collection(kind: str) -> Mock:
        if kind in collections:
            return collections[kind]
        collection = Mock()
        if kind == "prediction_metrics":
            side_effect: list[Any] = [_cursor(PER_FLIGHT_ROWS)]
            if percentile_error:
                side_effect.append(RuntimeError("$percentile unsupported"))  # type: ignore[arg-type]
            else:
                side_effect.append(_cursor(PERCENTILE_ROWS))
            collection.aggregate = AsyncMock(side_effect=side_effect)
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
        return collection

    repository.collection = Mock(side_effect=_collection)  # type: ignore[method-assign]
    return repository


@pytest.mark.asyncio
async def test_percentiles_attached_to_overall_rows_only():
    upserts: list[tuple[str, Any]] = []
    written = await materialize_prediction_metric_summaries(
        batch=BATCH,
        materialization_repository=_repository(upserts=upserts),
    )

    assert written == 3  # overall + phase + aircraft_type
    documents = {document["group_by"]: document for _kind, document in upserts}

    overall = documents["overall"]
    assert overall["horizontal_median_m"] == pytest.approx(90.0)
    assert overall["horizontal_p90_m"] == pytest.approx(150.0)
    assert overall["horizontal_p95_m"] == pytest.approx(170.0)
    assert overall["vertical_median_m"] == pytest.approx(9.0)
    assert overall["vertical_p95_m"] == pytest.approx(17.0)
    assert overall["percentile_method"] == "approximate"
    # MAE/RMSE 仍由聚合得出：MAE = 400/4 = 100；RMSE = sqrt(80000/4)
    assert overall["horizontal_mae_m"] == pytest.approx(100.0)
    assert overall["horizontal_rmse_m"] == pytest.approx((80000 / 4) ** 0.5)

    # 分组行本轮不带分位数（待下轮补）
    assert documents["phase"]["horizontal_median_m"] is None
    assert documents["phase"]["percentile_method"] is None


@pytest.mark.asyncio
async def test_percentile_failure_does_not_break_main_metrics():
    upserts: list[tuple[str, Any]] = []
    written = await materialize_prediction_metric_summaries(
        batch=BATCH,
        materialization_repository=_repository(upserts=upserts, percentile_error=True),
    )

    assert written == 3
    overall = {document["group_by"]: document for _kind, document in upserts}["overall"]
    assert overall["horizontal_mae_m"] == pytest.approx(100.0)
    assert overall["horizontal_median_m"] is None
    assert overall["percentile_method"] is None


def test_parser_defaults_for_metric_summaries():
    args = cli.build_parser().parse_args(["metric-summaries", "--batch", BATCH])

    assert args.command == "metric-summaries"
    assert args.batch == BATCH
    assert args.job_id is None
    assert args.algorithm_version == PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION
