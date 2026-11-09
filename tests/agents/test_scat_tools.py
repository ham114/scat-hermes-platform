"""Tests for structured results attached by SCAT Agent tools."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from agents import scat_tools as module
from scat.schemas.tool_inputs import TrackPointsInput


def test_collection_quality_tool_reuses_shared_flight_service_cache():
    assert module.gbar_data_tool._collection_quality_service is module.flight_service


@pytest.mark.asyncio
async def test_search_scat_flights_attaches_empty_structured_table(monkeypatch):
    connect = AsyncMock()
    search = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "week1", "limit": 20, "skip": 0},
            "data": {"count": 0, "total": 0, "has_more": False, "items": []},
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.flight_service, "search_flights", search)

    result = await module.search_scat_flights.ainvoke(
        {
            "batch": "week1",
            "aircraft_type": "a320",
            "adep": "essa",
            "takeoff_start": "2016-10-20T00:00:00Z",
            "takeoff_end": "2016-10-21T00:00:00Z",
            "sort_order": "desc",
        }
    )

    assert result["analysis_result"]["operation"] == "search_scat_flights"
    assert result["analysis_result"]["blocks"][1]["rows"] == []
    connect.assert_awaited_once_with()
    assert search.await_args.kwargs["aircraft_type"] == "A320"
    assert search.await_args.kwargs["adep"] == "ESSA"
    assert search.await_args.kwargs["start_time"] == datetime(
        2016, 10, 20, tzinfo=UTC
    )
    assert search.await_args.kwargs["end_time"] == datetime(
        2016, 10, 21, tzinfo=UTC
    )
    assert search.await_args.kwargs["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_scat_overview_and_track_tools_attach_structured_results(monkeypatch):
    connect = AsyncMock()
    overview = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "week1", "external_id": "1394"},
            "data": {
                "flight": {
                    "source": {"external_id": 1394},
                    "identity": {"callsign": "IBK8WB"},
                },
                "trajectory": {"vertical_mode_counts": {}},
            },
        }
    )
    track = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "week1", "external_id": "1394", "every_nth": 2},
            "data": {
                "flight": {"source": {"external_id": 1394}},
                "pagination": {"total_points": 1, "has_more": False},
                "points": [
                    {
                        "sequence": 0,
                        "timestamp": "2016-10-20T20:00:00Z",
                        "position": {"altitude_m": 1000.0},
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.flight_service, "get_flight_overview", overview)
    monkeypatch.setattr(module.flight_service, "get_track_points", track)

    overview_result = await module.get_scat_flight_overview.ainvoke(
        {"batch": "week1", "external_id": "1394"}
    )
    track_result = await module.get_scat_track_points.ainvoke(
        {
            "batch": "week1",
            "external_id": "1394",
            "start_time": "2016-10-20T20:00:00Z",
            "every_nth": 2,
            "fields": ["position"],
        }
    )

    assert overview_result["analysis_result"]["operation"] == "get_scat_flight_overview"
    assert track_result["analysis_result"]["schema_version"] == "1.2"
    assert track_result["analysis_result"]["start_time"] == "2016-10-20T20:00:00Z"
    kwargs = track.await_args.kwargs
    assert kwargs["start_time"] == datetime(2016, 10, 20, 20, tzinfo=UTC)
    assert kwargs["every_nth"] == 2
    assert kwargs["fields"] == {"position"}


@pytest.mark.asyncio
async def test_scat_quality_tool_exposes_shared_model_repair_report(monkeypatch):
    connect = AsyncMock()
    inspect = AsyncMock(
        return_value={
            "model_ready": True,
            "error_code": None,
            "message": "质量检查完成",
            "quality": {
                "detected_problem_values": 2,
                "repaired_values": 2,
                "unrepaired_values": 0,
                "features": [
                    {
                        "feature": "vx",
                        "missing_or_non_finite": 1,
                        "out_of_range": 0,
                        "spike": 1,
                        "interpolated": 2,
                        "extrapolated": 0,
                        "unrepaired": 0,
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.gbar_data_tool, "inspect_quality", inspect)

    result = await module.inspect_scat_model_data_quality.ainvoke(
        {
            "batch": "week1",
            "external_id": "1394",
            "end_time": "2016-10-20T20:00:00Z",
            "source_limit": 300,
        }
    )

    assert result["status"] == "ok"
    assert result["data"]["model_ready"] is True
    assert result["data"]["quality"]["repaired_values"] == 2
    assert inspect.await_args.kwargs["end_time"] == "2016-10-20T20:00:00+00:00"
    assert inspect.await_args.kwargs["source_limit"] == 300


@pytest.mark.asyncio
async def test_scat_collection_quality_tool_runs_one_aggregate_operation(monkeypatch):
    connect = AsyncMock()
    summarize = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": None},
            "data": {
                "scope": "all_scat_flights",
                "summary": {
                    "flight_count": 13_138,
                    "flights_with_missing": 12,
                    "flights_with_outliers": 5,
                    "flights_with_spikes": 3,
                },
                "temporal_spikes_included": True,
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(
        module.gbar_data_tool,
        "inspect_collection_quality",
        summarize,
    )

    result = await module.inspect_scat_collection_data_quality.ainvoke({})

    assert result["status"] == "ok"
    assert result["data"]["summary"]["flight_count"] == 13_138
    assert result["data"]["temporal_spikes_included"] is True
    summarize.assert_awaited_once_with(batch=None)


@pytest.mark.asyncio
async def test_scat_quality_problem_flight_tool_attaches_table(monkeypatch):
    connect = AsyncMock()
    list_flights = AsyncMock(
        return_value={
            "status": "ok",
            "query": {
                "batch": None,
                "limit": 20,
                "offset": 0,
                "problem_type": "missing",
                "sort_by": "missing_point_count",
            },
            "data": {
                "total": 120,
                "offset": 0,
                "limit": 20,
                "returned": 1,
                "has_more": True,
                "items": [
                    {
                        "batch": "scat20161015_20161021",
                        "external_id": "100016",
                        "original_filename": "100016.json",
                        "trajectory_key": "scat20161015_20161021/100016",
                        "callsign": "SAS016",
                        "point_count": 900,
                        "missing_point_count": 3,
                        "outlier_point_count": 7,
                        "spike_value_count": 2,
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(
        module.flight_service,
        "list_model_data_quality_problem_flights",
        list_flights,
    )

    result = await module.list_scat_quality_problem_flights.ainvoke(
        {
            "limit": 20,
            "problem_type": "missing",
            "sort_by": "missing_point_count",
        }
    )

    assert result["status"] == "ok"
    analysis = result["analysis_result"]
    assert analysis["operation"] == "list_scat_quality_problem_flights"
    assert analysis["blocks"][0]["type"] == "metrics"
    table = analysis["blocks"][1]
    assert table["type"] == "table"
    assert table["rows"][0]["external_id"] == "100016"
    assert table["rows"][0]["original_filename"] == "100016.json"
    list_flights.assert_awaited_once_with(
        batch=None,
        limit=20,
        offset=0,
        problem_type="missing",
        sort_by="missing_point_count",
    )


@pytest.mark.asyncio
async def test_scat_quality_clean_flight_tool_returns_filenames(monkeypatch):
    connect = AsyncMock()
    list_flights = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": None, "limit": 5, "offset": 0},
            "data": {
                "total": 10_552,
                "offset": 0,
                "limit": 5,
                "returned": 1,
                "has_more": True,
                "items": [
                    {
                        "batch": "scat20161015_20161021",
                        "external_id": "100002",
                        "original_filename": "100002.json",
                        "trajectory_key": "scat20161015_20161021/100002",
                        "callsign": "SAS001",
                        "point_count": 800,
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(
        module.flight_service,
        "list_model_data_quality_clean_flights",
        list_flights,
    )

    result = await module.list_scat_quality_clean_flights.ainvoke({"limit": 5})

    assert result["status"] == "ok"
    analysis = result["analysis_result"]
    assert analysis["operation"] == "list_scat_quality_clean_flights"
    assert analysis["blocks"][1]["rows"][0]["original_filename"] == "100002.json"
    list_flights.assert_awaited_once_with(batch=None, limit=5, offset=0)


def test_scat_track_input_rejects_naive_and_reversed_times():
    with pytest.raises(ValueError, match="时区"):
        TrackPointsInput(
            external_id="1394",
            start_time="2016-10-20T20:00:00",
        )

    with pytest.raises(ValueError, match="必须早于"):
        TrackPointsInput(
            external_id="1394",
            start_time="2016-10-20T21:00:00Z",
            end_time="2016-10-20T20:00:00Z",
        )


@pytest.mark.asyncio
async def test_airport_stats_tool_attaches_metrics_and_table(monkeypatch):
    connect = AsyncMock()
    stats = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "scat20161015_20161021", "limit": 20},
            "data": {
                "statistics": {
                    "flight_count": 10,
                    "airport_count": 2,
                    "with_atd_count": 8,
                    "mean_delay_minutes": 12.5,
                    "delayed_count": 2,
                    "delayed_rate": 0.25,
                    "delay_threshold_minutes": 15,
                },
                "airports": [
                    {
                        "airport": "EKCH",
                        "flight_count": 6,
                        "with_atd_count": 5,
                        "mean_delay_minutes": 10.0,
                        "max_delay_minutes": 25.0,
                        "min_delay_minutes": 0.0,
                        "delayed_count": 1,
                        "delayed_rate": 0.2,
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.flight_service, "get_airport_stats", stats)

    result = await module.get_scat_airport_stats.ainvoke({"batch": "scat20161015_20161021"})

    analysis = result["analysis_result"]
    assert analysis["operation"] == "get_scat_airport_stats"
    assert analysis["blocks"][0]["type"] == "metrics"
    assert analysis["blocks"][1]["rows"][0]["airport"] == "EKCH"
    stats.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase_stats_tool_attaches_phase_table(monkeypatch):
    connect = AsyncMock()
    phases = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "b1", "external_id": "100016"},
            "data": {
                "total_points": 10,
                "phases": [
                    {
                        "vertical_mode": 1,
                        "phase_name": "climb",
                        "point_count": 4,
                        "point_ratio": 0.4,
                        "start_time": "2016-10-20T11:40:02Z",
                        "end_time": "2016-10-20T11:42:02Z",
                        "duration_seconds": 120.0,
                        "flight_level": {
                            "minimum": 140.0,
                            "maximum": 350.0,
                            "average": 250.0,
                        },
                        "ground_speed": {
                            "average_mps": 200.0,
                            "maximum_mps": 220.0,
                        },
                        "absolute_vertical_rate": {
                            "average_fpm": 1500.0,
                            "maximum_fpm": 2500.0,
                        },
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.flight_service, "get_flight_phase_stats", phases)

    result = await module.get_scat_flight_phase_stats.ainvoke(
        {"batch": "b1", "external_id": "100016"}
    )

    analysis = result["analysis_result"]
    assert analysis["operation"] == "get_scat_flight_phase_stats"
    table = analysis["blocks"][1]
    assert table["rows"][0]["phase_name"] == "climb"
    assert table["rows"][0]["duration_seconds"] == 120.0


@pytest.mark.asyncio
async def test_prediction_error_tool_attaches_error_metrics(monkeypatch):
    connect = AsyncMock()
    errors = AsyncMock(
        return_value={
            "status": "ok",
            "query": {"batch": "b1", "external_id": "100016"},
            "data": {
                "snapshot_time": "2016-10-20T11:30:00",
                "route_point_count": 2,
                "track_point_count": 100,
                "matched_fix_count": 1,
                "unmatched_fix_count": 1,
                "statistics": {
                    "horizontal_error": {
                        "mean_m": 320.5,
                        "median_m": 320.5,
                        "max_m": 320.5,
                        "rmse_m": 320.5,
                    },
                    "vertical_error": {"mean_abs_m": 15.2, "max_abs_m": 15.2},
                    "time_error": {
                        "mean_abs_s": 1.0,
                        "mean_signed_s": 1.0,
                        "max_abs_s": 1.0,
                    },
                },
                "fixes": [
                    {
                        "fix_name": "Z9034",
                        "fix_kind": "RP",
                        "eto": "2016-10-20T11:33:59",
                        "time_error_s": 1.0,
                        "horizontal_error_m": 320.5,
                        "vertical_error_m": 15.2,
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.flight_service, "get_prediction_error_stats", errors)

    result = await module.get_scat_prediction_error_stats.ainvoke(
        {"batch": "b1", "external_id": "100016"}
    )

    analysis = result["analysis_result"]
    assert analysis["operation"] == "get_scat_prediction_error_stats"
    metrics = analysis["blocks"][0]
    assert metrics["type"] == "metrics"
    assert analysis["blocks"][1]["rows"][0]["horizontal_error_m"] == 320.5
