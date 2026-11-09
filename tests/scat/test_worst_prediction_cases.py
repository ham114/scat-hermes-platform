"""M5-07 最差案例检索测试：服务层只读查询与 agent 工具接线。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agents import eval_tools as tools
from agents.eval_tools import get_worst_prediction_cases
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import PREDICTION_METRICS_ALGORITHM_VERSION
from scat.services.materialization_service import MaterializationService

BATCH = "scat20161015_20161021"

SAMPLES: list[dict[str, Any]] = [
    {
        "batch": BATCH,
        "external_id": "107828",
        "snapshot_time": "2016-10-19T13:54:24.799000+00:00",
        "horizon_s": 300.0,
        "horizontal_error_m": 1252321.0,
        "vertical_error_m": 0.0,
        "along_track_error_m": -1110000.0,
        "cross_track_error_m": 590500.0,
        "is_valid": True,
    }
]
PHASE_ROWS = [{"external_id": "107828", "dominant_phase": "high_cruise"}]
FEATURE_ROWS = [{"external_id": "107828", "aircraft_type": "A388"}]


def _cursor(rows: list[dict[str, Any]]) -> Mock:
    cursor = Mock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def _service() -> tuple[MaterializationService, Mock, dict[str, Any]]:
    repository = MaterializationRepository(Mock())
    captured: dict[str, Any] = {}
    metrics = Mock()

    def _find(filters: dict[str, Any], projection: dict[str, Any] | None = None) -> Mock:
        captured["filters"] = filters
        return _cursor(SAMPLES)

    metrics.find = Mock(side_effect=_find)
    phases = Mock()
    phases.aggregate = AsyncMock(return_value=_cursor(PHASE_ROWS))
    features = Mock()
    features.aggregate = AsyncMock(return_value=_cursor(FEATURE_ROWS))
    collections = {
        "prediction_metrics": metrics,
        "phase_segments": phases,
        "flight_features": features,
    }
    repository.collection = Mock(side_effect=lambda kind: collections[kind])  # type: ignore[method-assign]
    return MaterializationService(repository), repository, captured


@pytest.mark.asyncio
async def test_service_lists_worst_samples_with_phase_and_aircraft():
    service, _repository, captured = _service()

    result = await service.list_worst_prediction_samples(
        batch=BATCH, metric="horizontal_error_m", horizon_s=300.0, limit=5
    )

    assert result["sample_count"] == 1
    sample = result["samples"][0]
    assert sample["external_id"] == "107828"
    assert sample["dominant_phase"] == "high_cruise"
    assert sample["aircraft_type"] == "A388"
    assert captured["filters"] == {
        "batch": BATCH,
        "source_version": "scat-import-2",
        "algorithm_version": PREDICTION_METRICS_ALGORITHM_VERSION,
        "is_valid": True,
        "horizontal_error_m": {"$ne": None},
        "horizon_s": 300.0,
    }


@pytest.mark.asyncio
async def test_service_rejects_unknown_metric():
    service, _repository, _captured = _service()

    with pytest.raises(ValueError, match="未知误差字段"):
        await service.list_worst_prediction_samples(batch=BATCH, metric="bogus")


@pytest.mark.asyncio
async def test_worst_cases_tool_maps_metric_and_delegates(monkeypatch: pytest.MonkeyPatch):
    service = Mock()
    service.list_worst_prediction_samples = AsyncMock(
        return_value={"batch": BATCH, "metric": "horizontal_error_m", "samples": SAMPLES}
    )
    monkeypatch.setattr(tools, "MaterializationService", lambda *a, **k: service)
    from agents import scat_tools

    monkeypatch.setattr(
        scat_tools, "flight_database", type("F", (), {"db": Mock(), "connect": AsyncMock()})()
    )

    result = await get_worst_prediction_cases.ainvoke(
        {"batch": BATCH, "metric": "cross", "horizon_s": 300, "limit": 3},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "ok"
    assert result["query"]["metric"] == "cross_track_error_m"
    assert result["data"]["samples"] == SAMPLES
    await_args = service.list_worst_prediction_samples.await_args
    assert await_args is not None
    assert await_args.kwargs["metric"] == "cross_track_error_m"
    assert await_args.kwargs["limit"] == 3
