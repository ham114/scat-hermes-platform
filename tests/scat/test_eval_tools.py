"""M5 评估域工具测试：单航班分段工具（物化优先 / 实时回退）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from agents import eval_tools as tools
from agents import scat_tools
from agents.eval_tools import get_scat_flight_phase_segments

BATCH = "scat20161015_20161021"
CONFIG: dict[str, Any] = {"configurable": {"thread_id": "t1", "user_id": "u1"}}
TRACK: list[dict[str, Any]] = [
    {
        "timestamp": "2016-10-16T16:00:00",
        "lat": 59.0,
        "lon": 18.0,
        "measured_flight_level": 350.0,
        "vertical_mode": 0,
    },
    {
        "timestamp": "2016-10-16T16:05:00",
        "lat": 59.05,
        "lon": 18.0,
        "measured_flight_level": 350.0,
        "vertical_mode": 0,
    },
    {
        "timestamp": "2016-10-16T16:10:00",
        "lat": 59.10,
        "lon": 18.0,
        "measured_flight_level": 60.0,
        "vertical_mode": 2,
    },
]


def _patch_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: Mock,
    flight: Mock | None = None,
) -> None:
    """替换评估域工具的模块级依赖：Service、航班仓库与（scat_tools 的）数据库。"""
    monkeypatch.setattr(tools, "MaterializationService", lambda *a, **k: service)
    if flight is not None:
        monkeypatch.setattr(tools, "ScatFlightRepository", lambda *a, **k: flight)
    fake_database = SimpleNamespace(db=Mock(name="flight_db"), connect=AsyncMock())
    monkeypatch.setattr(scat_tools, "flight_database", fake_database)


@pytest.mark.asyncio
async def test_single_flight_tool_prefers_materialized_result(monkeypatch: pytest.MonkeyPatch):
    document = {
        "assembly": "phase_segments",
        "batch": BATCH,
        "external_id": "100002",
        "point_count": 524,
        "segment_count": 21,
        "dominant_phase": "high_cruise",
        "phase_seconds": {"high_cruise": 100.0},
        "segments": [{"index": 0, "phase": "high_cruise"}],
    }
    service = Mock()
    service.get_flight_phase_segments = AsyncMock(return_value=document)
    flight = Mock()
    flight.get_trajectory_points_for_phases = AsyncMock()
    _patch_context(monkeypatch, service=service, flight=flight)

    result = await get_scat_flight_phase_segments.ainvoke(
        {"batch": BATCH, "external_id": "100002"},
        config=CONFIG,
    )

    assert result["status"] == "ok"
    assert result["data"]["source"] == "materialized"
    assert result["data"]["segment_count"] == 21
    flight.get_trajectory_points_for_phases.assert_not_called()


@pytest.mark.asyncio
async def test_single_flight_tool_falls_back_to_live_computation(monkeypatch: pytest.MonkeyPatch):
    service = Mock()
    service.get_flight_phase_segments = AsyncMock(return_value=None)
    flight = Mock()
    flight.get_trajectory_points_for_phases = AsyncMock(return_value=TRACK)
    _patch_context(monkeypatch, service=service, flight=flight)

    result = await get_scat_flight_phase_segments.ainvoke(
        {"batch": BATCH, "external_id": "100002"},
        config=CONFIG,
    )

    assert result["status"] == "ok"
    assert result["data"]["source"] == "live"
    assert result["data"]["point_count"] == len(TRACK)
    assert result["data"]["segment_count"] >= 1
    assert sum((result["data"]["phase_seconds"] or {}).values()) > 0


@pytest.mark.asyncio
async def test_single_flight_tool_requires_external_id():
    # 空 external_id 在输入模型（min_length=1）就被拦住，不会进入工具主体。
    with pytest.raises(ValidationError):
        await get_scat_flight_phase_segments.ainvoke(
            {"batch": BATCH, "external_id": ""},
            config=CONFIG,
        )
