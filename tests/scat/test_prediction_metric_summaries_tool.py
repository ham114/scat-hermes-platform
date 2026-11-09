"""评估汇总工具测试：服务层只读查询与 agent 工具接线（get_prediction_metric_summaries）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agents import eval_tools as tools
from agents.eval_tools import get_prediction_metric_summaries
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    DEFAULT_SOURCE_VERSION,
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
)
from scat.services.materialization_service import MaterializationService

BATCH = "scat20161015_20161021"

ROWS: list[dict[str, Any]] = [
    {
        "batch": BATCH,
        "horizon_s": 300.0,
        "group_by": "phase",
        "group_value": "high_cruise",
        "sample_count": 136924,
        "flight_count": 4458,
        "horizontal_mae_m": 3740.0,
    }
]


def _cursor(rows: list[dict[str, Any]], captured: dict[str, Any]) -> Mock:
    cursor = Mock()
    cursor.sort.return_value = cursor

    def _limit(value: int) -> Mock:
        captured["limit"] = value
        return cursor

    cursor.limit.side_effect = _limit
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def _service() -> tuple[MaterializationService, dict[str, Any]]:
    repository = MaterializationRepository(Mock())
    captured: dict[str, Any] = {}
    collection = Mock()

    def _find(filters: dict[str, Any], projection: dict[str, Any] | None = None) -> Mock:
        captured["filters"] = filters
        return _cursor(ROWS, captured)

    collection.find = Mock(side_effect=_find)
    repository.collection = Mock(return_value=collection)  # type: ignore[method-assign]
    return MaterializationService(repository), captured


@pytest.mark.asyncio
async def test_service_lists_metric_summaries_with_filters_and_limit_clamp():
    service, captured = _service()

    result = await service.list_metric_summaries(
        batch=BATCH,
        group_by="phase",
        horizon_s=300.0,
        limit=999,
    )

    assert captured["filters"] == {
        "batch": BATCH,
        "source_version": DEFAULT_SOURCE_VERSION,
        "algorithm_version": PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
        "group_by": "phase",
        "horizon_s": 300.0,
    }
    assert captured["limit"] == 500
    assert result["batch"] == BATCH
    assert result["group_by"] == "phase"
    assert result["horizon_s"] == 300.0
    assert result["record_count"] == 1
    assert result["summaries"] == ROWS


@pytest.mark.asyncio
async def test_service_metric_summaries_defaults_cover_all_groups_and_horizons():
    service, captured = _service()

    await service.list_metric_summaries(batch=BATCH)

    assert captured["filters"] == {
        "batch": BATCH,
        "source_version": DEFAULT_SOURCE_VERSION,
        "algorithm_version": PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    }
    assert captured["limit"] == 200


@pytest.mark.asyncio
async def test_metric_summaries_tool_maps_arguments_and_delegates(monkeypatch: pytest.MonkeyPatch):
    service = Mock()
    service.list_metric_summaries = AsyncMock(
        return_value={
            "batch": BATCH,
            "group_by": "aircraft_type",
            "horizon_s": 300.0,
            "record_count": 1,
            "summaries": ROWS,
        }
    )
    monkeypatch.setattr(tools, "MaterializationService", lambda *a, **k: service)
    from agents import scat_tools

    monkeypatch.setattr(
        scat_tools, "flight_database", type("F", (), {"db": Mock(), "connect": AsyncMock()})()
    )

    result = await get_prediction_metric_summaries.ainvoke(
        {"batch": BATCH, "group_by": "aircraft_type", "horizon_s": 300, "limit": 42},
        config={"configurable": {"thread_id": "t1", "user_id": "u1"}},
    )

    assert result["status"] == "ok"
    assert result["operation"] == "get_prediction_metric_summaries"
    assert result["query"] == {
        "batch": BATCH,
        "group_by": "aircraft_type",
        "horizon_s": 300,
        "limit": 42,
    }
    assert result["data"]["record_count"] == 1
    assert result["data"]["summaries"] == ROWS
    await_args = service.list_metric_summaries.await_args
    assert await_args is not None
    assert await_args.kwargs["group_by"] == "aircraft_type"
    assert await_args.kwargs["horizon_s"] == 300
    assert await_args.kwargs["limit"] == 42
