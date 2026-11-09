"""M5-05 预测指标物化测试：口径映射、排除样本、增量与失败隔离、CLI 默认值。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from scat.materialization import cli
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    PREDICTION_METRICS_ALGORITHM_VERSION,
    materialize_batch_prediction_metrics,
    materialize_prediction_metrics,
)
from scat.prediction.alignment import PREDICTION_METRIC_VERSION

BATCH = "scat20161015_20161021"
EXTERNAL_ID = "100065"
T0 = datetime(2016, 10, 16, 16, 0, tzinfo=UTC)
HORIZONS = (60.0, 180.0, 300.0, 600.0)


def _plot(offset_s: int, lat: float, lon: float, altitude_m: float) -> dict[str, Any]:
    return {
        "timestamp": (T0 + timedelta(seconds=offset_s)).isoformat(),
        "lat": lat,
        "lon": lon,
        "altitude_m": altitude_m,
    }


def _route_point(offset_s: int, lat: float, lon: float, altitude_m: float) -> dict[str, Any]:
    return {
        "eto": (T0 + timedelta(seconds=offset_s)).isoformat(),
        "lat": lat,
        "lon": lon,
        "afl_value": altitude_m / 30.48,
        "afl_unit": "F",
    }


# 实飞：0～600 s 每 10 s 一点（足够密，可算航向→能分解沿航向/横向误差）
PLOTS = [_plot(offset, 59.0 + offset * 0.0001, 18.0, 10000.0) for offset in range(0, 601, 10)]
SNAPSHOT = {
    "time_stamp": T0.isoformat(),
    "route": [
        _route_point(60, 59.05, 18.0, 10500.0),
        _route_point(300, 59.30, 18.0, 11000.0),
    ],
}
EVALUATION_INPUTS = {"plots": PLOTS, "snapshots": [SNAPSHOT]}


def _flight_repository(*, inputs_by_id: dict[str, dict[str, Any] | None]) -> Mock:
    repository = Mock()
    repository.get_evaluation_inputs = AsyncMock(
        side_effect=lambda *, batch, external_id, limit=20000: inputs_by_id.get(str(external_id))
    )
    return repository


def _materialization_repository(
    *,
    upserts: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
) -> MaterializationRepository:
    repository = MaterializationRepository(Mock())
    collection = Mock()
    collection.replace_one = AsyncMock(
        side_effect=lambda f, d, **kw: upserts.append((f, d)) if upserts is not None else None
    )
    collection.bulk_write = AsyncMock(
        side_effect=lambda operations, **kw: (
            upserts.extend((operation._filter, operation._doc) for operation in operations)
            if upserts is not None
            else None
        )
    )
    repository.collection = Mock(return_value=collection)  # type: ignore[method-assign]
    repository.existing_values = AsyncMock(return_value=set())  # type: ignore[method-assign]
    return repository


@pytest.mark.asyncio
async def test_materialize_prediction_metrics_writes_valid_samples_only_by_default():
    upserts: list[tuple[dict, dict]] = []
    written = await materialize_prediction_metrics(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(inputs_by_id={EXTERNAL_ID: EVALUATION_INPUTS}),
        materialization_repository=_materialization_repository(upserts=upserts),
        horizons=HORIZONS,
        generated_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
    )

    # 60/180/300 s 在预测覆盖范围内（有效）；600 s 超出预测末点（默认不写）
    assert written == 3
    assert len(upserts) == 3
    by_horizon = {document["horizon_s"]: document for _, document in upserts}
    assert set(by_horizon) == {60.0, 180.0, 300.0}

    valid = by_horizon[60.0]
    assert valid["is_valid"] is True
    assert valid["exclusion_reason"] is None
    assert valid["horizontal_error_m"] > 0
    assert valid["vertical_error_m"] is not None
    assert valid["metric_version"] == PREDICTION_METRIC_VERSION
    # 水平误差 ≈ √(沿航向² + 横向²)
    assert valid["horizontal_error_m"] == pytest.approx(
        (valid["along_track_error_m"] ** 2 + valid["cross_track_error_m"] ** 2) ** 0.5,
        rel=0.01,
    )

    filter_doc, _ = upserts[0]
    assert filter_doc == {
        "batch": BATCH,
        "external_id": EXTERNAL_ID,
        "snapshot_time": T0,
        "horizon_s": filter_doc["horizon_s"],
        "source_version": "scat-import-2",
        "algorithm_version": PREDICTION_METRICS_ALGORITHM_VERSION,
    }


@pytest.mark.asyncio
async def test_materialize_prediction_metrics_can_include_excluded_samples():
    upserts: list[tuple[dict, dict]] = []
    written = await materialize_prediction_metrics(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(inputs_by_id={EXTERNAL_ID: EVALUATION_INPUTS}),
        materialization_repository=_materialization_repository(upserts=upserts),
        horizons=HORIZONS,
        include_excluded=True,
    )

    assert written == len(HORIZONS)
    by_horizon = {document["horizon_s"]: document for _, document in upserts}
    excluded = by_horizon[600.0]
    assert excluded["is_valid"] is False
    assert excluded["exclusion_reason"] == "horizon_not_covered"
    assert excluded["horizontal_error_m"] is None


@pytest.mark.asyncio
async def test_materialize_prediction_metrics_returns_zero_without_snapshots():
    """无预测快照不是错误：无样本可评估，返回 0 条。"""
    written = await materialize_prediction_metrics(
        batch=BATCH,
        external_id=EXTERNAL_ID,
        flight_repository=_flight_repository(
            inputs_by_id={EXTERNAL_ID: {"plots": PLOTS, "snapshots": []}}
        ),
        materialization_repository=_materialization_repository(),
    )

    assert written == 0


@pytest.mark.asyncio
async def test_materialize_prediction_metrics_rejects_missing_flight():
    with pytest.raises(ValueError, match="航班不存在"):
        await materialize_prediction_metrics(
            batch=BATCH,
            external_id="no-such",
            flight_repository=_flight_repository(inputs_by_id={}),
            materialization_repository=_materialization_repository(),
        )


@pytest.mark.asyncio
async def test_batch_prediction_metrics_isolates_failures_and_uses_kind():
    upserts: list[tuple[dict, dict]] = []
    repository = _materialization_repository(upserts=upserts)
    result = await materialize_batch_prediction_metrics(
        batch=BATCH,
        flight_repository=_flight_repository(
            inputs_by_id={EXTERNAL_ID: EVALUATION_INPUTS, "100066": None}
        ),
        materialization_repository=repository,
        external_ids=[EXTERNAL_ID, "100066"],
    )

    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed"][0]["external_id"] == "100066"
    assert "航班不存在" in result["failed"][0]["error"]
    # 默认只写有效样本：HORIZONS_S 中 60/180/300 s 有效，600/900/1200 s 超出预测末点
    assert len(upserts) == 3


def test_parser_defaults_for_prediction_metrics():
    args = cli.build_parser().parse_args(["prediction-metrics", "--batch", BATCH])

    assert args.command == "prediction-metrics"
    assert args.algorithm_version == PREDICTION_METRICS_ALGORITHM_VERSION
    assert args.force is False
    assert args.rebuild is False
    assert args.page_size == 200
