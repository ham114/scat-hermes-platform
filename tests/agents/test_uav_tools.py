"""Tests for the UAV tools exposed to the aviation Agent."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from agents import uav_tools as module
from agents.scat_agent import AVIATION_AGENT_INSTRUCTIONS, aviation_tools
from uav.schemas.tool_inputs import UavSamplesInput, UavSearchFlightsInput


@pytest.mark.asyncio
async def test_list_uav_models_connects_and_calls_service(monkeypatch):
    connect = AsyncMock()
    list_models = AsyncMock(
        return_value={"status": "ok", "data": {"items": [{"model": "0001"}]}}
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.uav_flight_service, "list_models", list_models)

    result = await module.list_uav_models.ainvoke({})

    assert result["status"] == "ok"
    assert result["analysis_result"]["schema_version"] == "1.0"
    assert result["analysis_result"]["dataset"] == "uav"
    connect.assert_awaited_once_with()
    list_models.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_search_uav_flights_passes_validated_filters(monkeypatch):
    connect = AsyncMock()
    list_flights = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"limit": 5, "offset": 0},
            "data": {
                "pagination": {
                    "offset": 0,
                    "limit": 5,
                    "returned": 0,
                    "total": 0,
                    "has_more": False,
                },
                "items": [],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.uav_flight_service, "list_flights", list_flights)

    result = await module.search_uav_flights.ainvoke(
        {
            "model": "0001",
            "mission": "dtc",
            "takeoff_start": "2023-03-30T00:00:00Z",
            "limit": 5,
        }
    )

    kwargs = list_flights.await_args.kwargs
    assert kwargs["model"] == "0001"
    assert kwargs["mission"] == "DTC"
    assert kwargs["start_time"] == datetime(2023, 3, 30, tzinfo=UTC)
    assert kwargs["limit"] == 5
    assert result["analysis_result"]["operation"] == "search_uav_flights"
    assert result["analysis_result"]["blocks"][1]["rows"] == []


@pytest.mark.asyncio
async def test_get_uav_flight_overview_attaches_channel_statistics(monkeypatch):
    connect = AsyncMock()
    get_flight = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "external_id": "flight-1",
                "model": "0001",
                "summary": {},
                "channel_stats": [
                    {"key": "cmp_comb_alt", "min": 1, "max": 2, "avg": 1.5, "missing": 0}
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.uav_flight_service, "get_flight", get_flight)

    result = await module.get_uav_flight_overview.ainvoke({"external_id": "flight-1"})

    assert result["analysis_result"]["operation"] == "get_uav_flight_overview"
    assert result["analysis_result"]["blocks"][-1]["rows"][0]["unit"] == "m"
    get_flight.assert_awaited_once_with("flight-1")


@pytest.mark.asyncio
async def test_get_uav_samples_passes_exact_channels_and_attaches_timeseries(monkeypatch):
    connect = AsyncMock()
    get_samples = AsyncMock(
        return_value={
            "status": "ok",
            "query": {
                "start": 0,
                "limit": 100,
                "every_nth": 2,
                "fields": ["position"],
                "channels": ["cmp_comb_alt"],
            },
            "data": {
                "external_id": "flight-1",
                "start_time": "2023-03-30T04:27:40Z",
                "limit": 100,
                "every_nth": 2,
                "returned": 2,
                "has_more": False,
                "samples": [
                    {"seq": 0, "t_s": 0.0, "cmp_comb_lon": 116.0, "cmp_comb_lat": 28.0, "cmp_comb_alt": 10.0},
                    {"seq": 2, "t_s": 2.0, "cmp_comb_lon": 116.02, "cmp_comb_lat": 28.02, "cmp_comb_alt": 12.0},
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.uav_flight_service, "get_samples", get_samples)

    result = await module.get_uav_samples.ainvoke(
        {
            "external_id": "flight-1",
            "every_nth": 2,
            "fields": ["position"],
            "channels": ["cmp_comb_alt"],
        }
    )

    assert result["analysis_result"]["schema_version"] == "1.3"
    assert result["analysis_result"]["start_time"] == "2023-03-30T04:27:40Z"
    trajectory_blocks = [
        block
        for block in result["analysis_result"]["blocks"]
        if block["type"] == "trajectory_2d"
    ]
    assert len(trajectory_blocks) == 1
    assert trajectory_blocks[0]["x_key"] == "cmp_comb_lon"
    assert trajectory_blocks[0]["y_key"] == "cmp_comb_lat"
    assert len(trajectory_blocks[0]["points"]) == 2
    kwargs = get_samples.await_args.kwargs
    assert kwargs["channels"] == {"cmp_comb_alt"}
    assert kwargs["fields"] == {"position"}


def test_agent_instructions_guide_two_dimensional_trajectory():
    from agents.scat_agent import AVIATION_AGENT_INSTRUCTIONS

    assert "二维轨迹图" in AVIATION_AGENT_INSTRUCTIONS
    assert "fields=[\"position\"]" in AVIATION_AGENT_INSTRUCTIONS
    assert "cmp_comb_lon" in AVIATION_AGENT_INSTRUCTIONS
    assert "自动渲染二维轨迹图" in AVIATION_AGENT_INSTRUCTIONS


def test_uav_samples_input_rejects_unknown_or_cross_group_channel():
    with pytest.raises(ValueError, match="不支持"):
        UavSamplesInput(external_id="flight-1", channels={"bogus"})

    with pytest.raises(ValueError, match="必须属于"):
        UavSamplesInput(
            external_id="flight-1",
            fields={"position"},
            channels={"cmp_pitch"},
        )


def test_uav_search_input_rejects_naive_or_reversed_times():
    with pytest.raises(ValueError, match="时区"):
        UavSearchFlightsInput(takeoff_start="2023-03-30T00:00:00")

    with pytest.raises(ValueError, match="必须早于"):
        UavSearchFlightsInput(
            takeoff_start="2023-03-31T00:00:00Z",
            takeoff_end="2023-03-30T00:00:00Z",
        )


def test_aviation_agent_registers_both_dataset_toolsets():
    names = {item.name for item in aviation_tools}

    assert "get_scat_batch_overview" in names
    assert "list_uav_models" in names
    assert "get_uav_flight_overview" in names
    assert len(names) == len(aviation_tools)
    assert "两个数据集" in AVIATION_AGENT_INSTRUCTIONS
    assert "禁止用空字符串、猜测值或" in AVIATION_AGENT_INSTRUCTIONS
    assert "channels 精确通道筛选" in AVIATION_AGENT_INSTRUCTIONS


@pytest.mark.asyncio
async def test_channel_stats_tool_attaches_table(monkeypatch):
    connect = AsyncMock()
    stats = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"channels": ["cmp_comb_alt"], "model": None, "flight_kind": None},
            "data": {
                "flights_included": 42,
                "items": [
                    {
                        "channel": "cmp_comb_alt",
                        "label": "组合高度",
                        "unit": "m",
                        "group": "position",
                        "flight_count": 42,
                        "total_samples": 300000,
                        "weighted_mean": 800.5,
                        "min": 10.0,
                        "max": 2500.0,
                        "missing_total": 3,
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.uav_flight_service, "get_channel_stats", stats)

    result = await module.get_uav_channel_stats.ainvoke(
        {"channels": {"cmp_comb_alt"}}
    )

    analysis = result["analysis_result"]
    assert analysis["operation"] == "get_uav_channel_stats"
    assert analysis["blocks"][0]["type"] == "metrics"
    assert analysis["blocks"][1]["rows"][0]["label"] == "组合高度"
    stats.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_vs_anomaly_tool_attaches_comparison_table(monkeypatch):
    connect = AsyncMock()
    compare = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"channels": ["cmp_pitch"], "model": None},
            "data": {
                "items": [
                    {
                        "channel": "cmp_pitch",
                        "label": "俯仰角",
                        "unit": "deg",
                        "group": "attitude",
                        "normal": {
                            "flight_count": 39,
                            "total_samples": 250000,
                            "weighted_mean": 2.0,
                            "min": -5.0,
                            "max": 8.0,
                            "missing_total": 0,
                        },
                        "anomaly": {
                            "flight_count": 3,
                            "total_samples": 9000,
                            "weighted_mean": 6.0,
                            "min": -2.0,
                            "max": 20.0,
                            "missing_total": 0,
                        },
                        "delta_mean": 4.0,
                        "delta_relative": 2.0,
                    }
                ]
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(
        module.uav_flight_service, "get_normal_vs_anomaly_stats", compare
    )

    result = await module.get_uav_normal_vs_anomaly_stats.ainvoke(
        {"channels": {"cmp_pitch"}}
    )

    analysis = result["analysis_result"]
    assert analysis["operation"] == "get_uav_normal_vs_anomaly_stats"
    row = analysis["blocks"][0]["rows"][0]
    assert row["normal_mean"] == 2.0
    assert row["anomaly_mean"] == 6.0
    assert row["delta_relative"] == 2.0


@pytest.mark.asyncio
async def test_phase_stats_tool_attaches_phase_table(monkeypatch):
    connect = AsyncMock()
    phases = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"external_id": "20230330122740_DTC_53.1394B"},
            "data": {
                "total_points": 500,
                "phase_threshold_mps": 1.0,
                "phases": [
                    {
                        "phase": "climb",
                        "point_count": 100,
                        "point_ratio": 0.2,
                        "start_t_s": 10.0,
                        "end_t_s": 60.0,
                        "duration_seconds": 50.0,
                        "altitude": {
                            "minimum_m": 100.0,
                            "maximum_m": 800.0,
                            "average_m": 450.0,
                        },
                        "ground_speed": {
                            "average_mps": 30.0,
                            "maximum_mps": 35.0,
                        },
                        "vertical_speed": {"average_mps": 3.5},
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(
        module.uav_flight_service, "get_flight_phase_stats", phases
    )

    result = await module.get_uav_phase_stats.ainvoke(
        {"external_id": "20230330122740_DTC_53.1394B"}
    )

    analysis = result["analysis_result"]
    assert analysis["operation"] == "get_uav_phase_stats"
    row = analysis["blocks"][1]["rows"][0]
    assert row["phase"] == "climb"
    assert row["average_altitude_m"] == 450.0
