"""自研预测工具测试：PredictionService 即时预测与 agent 工具接线（predict_scat_flight_trajectory）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from agents import prediction_tools as tools
from agents.prediction_tools import predict_scat_flight_trajectory
from scat.prediction.defaults import DEFAULT_PREDICTOR_ID
from scat.prediction.geo import local_to_geodetic_m
from scat.schemas.tool_inputs import (
    PredictFlightTrajectoryInput,
    PreparePredictionExperimentInput,
)
from scat.services.prediction_service import PredictionService

BATCH = "scat20161015_20161021"
T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)


def _plots(*, count: int = 60, step_s: float = 10.0) -> list[dict]:
    plots: list[dict] = []
    for index in range(count):
        lat, lon, _ = local_to_geodetic_m(
            120.0 * step_s * index,
            0.0,
            0.0,
            origin_lat=59.0,
            origin_lon=18.0,
            origin_alt_m=10000.0,
        )
        plots.append(
            {
                "timestamp": (T0 + timedelta(seconds=step_s * index)).isoformat(),
                "lat": lat,
                "lon": lon,
                "altitude_m": 10000.0,
            }
        )
    return plots


def _service(plots: list[dict] | None) -> PredictionService:
    repository = Mock()
    repository.get_evaluation_inputs = AsyncMock(
        return_value=None if plots is None else {"plots": plots, "snapshots": []}
    )
    return PredictionService(repository)


def test_input_schema_rejects_bad_values_and_applies_defaults() -> None:
    with pytest.raises(ValidationError):
        PredictFlightTrajectoryInput(external_id="")
    with pytest.raises(ValidationError):
        PredictFlightTrajectoryInput(external_id="f", horizons_s=[0.0])
    with pytest.raises(ValidationError):
        PredictFlightTrajectoryInput(external_id="f", horizons_s=[60, 180, 300, 600, 900, 1200, 10])
    with pytest.raises(ValidationError):
        PredictFlightTrajectoryInput(external_id="f", as_of="not-a-time")

    model = PredictFlightTrajectoryInput(external_id=100002)
    assert model.external_id == "100002"
    assert model.predictor_id == "kalman-cv"
    assert model.compare_actual is True
    assert model.horizons_s is None


def test_default_predictor_schema_matches_platform_default() -> None:
    """工具 schema 默认值必须与 DEFAULT_PREDICTOR_ID 同步（M6-10 切换默认时同步修改）。"""
    assert PredictFlightTrajectoryInput(external_id="f").predictor_id == DEFAULT_PREDICTOR_ID
    assert PreparePredictionExperimentInput(batch="b").predictor_id == DEFAULT_PREDICTOR_ID


@pytest.mark.asyncio
async def test_predict_flight_default_cutoff_predicts_beyond_track() -> None:
    service = _service(_plots())

    result = await service.predict_flight(batch=BATCH, external_id="100002")

    assert result is not None
    assert result["predictor"] == {"id": "kalman-cv", "version": "1", "schema_version": 1}
    assert result["cutoff_time"] == (T0 + timedelta(seconds=590)).isoformat()
    assert result["observed"]["point_count"] == 60
    assert result["horizons_s"] == [60.0, 180.0, 300.0, 600.0, 900.0, 1200.0]
    assert result["rows"][0]["target_time"] == (T0 + timedelta(seconds=650)).isoformat()
    # 盲预测（从轨迹末点向后）：目标时刻没有实飞真值
    assert all(row["actual"] is None and row["error_m"] is None for row in result["rows"])


@pytest.mark.asyncio
async def test_predict_flight_mid_flight_as_of_reports_actual_errors() -> None:
    service = _service(_plots())

    result = await service.predict_flight(
        batch=BATCH,
        external_id="100002",
        as_of=(T0 + timedelta(seconds=300)).isoformat(),
    )

    assert result is not None
    assert result["observed"]["point_count"] == 31
    first = result["rows"][0]
    assert first["horizon_s"] == 60.0
    assert first["actual"] is not None
    assert first["error_m"] is not None
    assert first["error_m"] < 500.0
    last = result["rows"][-1]
    assert last["actual"] is None
    assert last["actual_exclusion_reason"] == "out_of_range"


@pytest.mark.asyncio
async def test_predict_flight_custom_horizons_sorted_and_blind_mode() -> None:
    service = _service(_plots())

    result = await service.predict_flight(
        batch=BATCH,
        external_id="100002",
        horizons_s=[1200.0, 300.0],
        compare_actual=False,
    )

    assert result is not None
    assert [row["horizon_s"] for row in result["rows"]] == [300.0, 1200.0]
    assert result["actual_compared"] is False
    assert all(row["actual"] is None for row in result["rows"])


@pytest.mark.asyncio
async def test_predict_flight_unknown_predictor_lists_available() -> None:
    service = _service(_plots())

    with pytest.raises(ValueError, match="未知预测器.*可用"):
        await service.predict_flight(batch=BATCH, external_id="100002", predictor_id="nope")


@pytest.mark.asyncio
async def test_predict_flight_returns_none_when_flight_missing() -> None:
    service = _service(None)

    assert await service.predict_flight(batch=BATCH, external_id="nope") is None


@pytest.mark.asyncio
async def test_predict_flight_requires_enough_observed_points() -> None:
    service = _service(_plots())

    with pytest.raises(ValueError, match="观测点不足"):
        await service.predict_flight(
            batch=BATCH,
            external_id="100002",
            as_of=(T0 + timedelta(seconds=10)).isoformat(),
        )


@pytest.mark.asyncio
async def test_tool_maps_arguments_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Mock()
    service.predict_flight = AsyncMock(
        return_value={"batch": BATCH, "external_id": "100002", "rows": []}
    )
    monkeypatch.setattr(tools, "PredictionService", lambda *a, **k: service)
    from agents import scat_tools

    monkeypatch.setattr(
        scat_tools, "flight_database", type("F", (), {"db": Mock(), "connect": AsyncMock()})()
    )

    result = await predict_scat_flight_trajectory.ainvoke(
        {
            "batch": BATCH,
            "external_id": "100002",
            "predictor_id": "constant-velocity-heading",
            "as_of": "2016-10-20T12:05:00",
            "horizons_s": [300.0],
            "compare_actual": False,
        },
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "ok"
    assert result["operation"] == "predict_scat_flight_trajectory"
    assert result["query"]["as_of"] == "2016-10-20T12:05:00"
    await_args = service.predict_flight.await_args
    assert await_args is not None
    assert await_args.kwargs["external_id"] == "100002"
    assert await_args.kwargs["predictor_id"] == "constant-velocity-heading"
    assert await_args.kwargs["horizons_s"] == [300.0]
    assert await_args.kwargs["compare_actual"] is False


@pytest.mark.asyncio
async def test_tool_maps_not_found_result(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Mock()
    service.predict_flight = AsyncMock(return_value=None)
    monkeypatch.setattr(tools, "PredictionService", lambda *a, **k: service)
    from agents import scat_tools

    monkeypatch.setattr(
        scat_tools, "flight_database", type("F", (), {"db": Mock(), "connect": AsyncMock()})()
    )

    result = await predict_scat_flight_trajectory.ainvoke(
        {"batch": BATCH, "external_id": "999999"},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "not_found"
    assert "999999" in result["message"]
    assert result["data"] is None


@pytest.mark.asyncio
async def test_tool_maps_value_error_to_invalid_request(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Mock()
    service.predict_flight = AsyncMock(side_effect=ValueError("未知预测器 'nope'；可用: kalman-cv"))
    monkeypatch.setattr(tools, "PredictionService", lambda *a, **k: service)
    from agents import scat_tools

    monkeypatch.setattr(
        scat_tools, "flight_database", type("F", (), {"db": Mock(), "connect": AsyncMock()})()
    )

    result = await predict_scat_flight_trajectory.ainvoke(
        {"batch": BATCH, "external_id": "100002", "predictor_id": "nope"},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "invalid_request"
    assert "未知预测器" in result["message"]
