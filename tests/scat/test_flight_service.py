from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from scat.services.flight_service import ScatFlightService


@pytest.mark.asyncio
async def test_random_gbar_sample_normalizes_cutoff_and_reports_selected_ids():
    repository = Mock()
    repository.sample_gbar_flight_ids = AsyncMock(
        return_value={
            "external_ids": ["100002", "100014"],
            "eligible_count": 18,
        }
    )
    service = ScatFlightService(repository=repository)
    cutoff = datetime(2016, 10, 20, 20, 8, tzinfo=UTC)

    result = await service.sample_gbar_model_flights(
        batch=" batch ",
        sample_size=2,
        observation_points=300,
        aircraft_type="a320",
        adep="essa",
        end_time=cutoff,
    )

    sample_kwargs = repository.sample_gbar_flight_ids.await_args.kwargs
    assert sample_kwargs["batch"] == "batch"
    assert sample_kwargs["sample_size"] == 2
    assert sample_kwargs["observation_points"] == 300
    assert sample_kwargs["aircraft_type"] == "A320"
    assert sample_kwargs["adep"] == "ESSA"
    assert sample_kwargs["end_time"] == "2016-10-20T20:08:00.000000"
    assert sample_kwargs["future_points"] == 100
    assert result["status"] == "ok"
    assert result["data"]["eligible_count"] == 18
    assert result["data"]["external_ids"] == ["100002", "100014"]


@pytest.mark.asyncio
async def test_random_comparison_sample_enforces_shared_prerequisites_and_allows_fifty():
    repository = Mock()
    repository.sample_gbar_flight_ids = AsyncMock(
        return_value={
            "external_ids": ["100002", "100014", "100020"],
            "eligible_count": 64,
        }
    )
    service = ScatFlightService(repository=repository)

    result = await service.sample_comparison_model_flights(
        batch=" batch ",
        sample_size=3,
        min_track_points=120,
        aircraft_type="b738",
    )

    sample_kwargs = repository.sample_gbar_flight_ids.await_args.kwargs
    assert sample_kwargs["batch"] == "batch"
    assert sample_kwargs["sample_size"] == 3
    assert sample_kwargs["maximum_sample_size"] == 50
    assert sample_kwargs["observation_points"] == 71
    assert sample_kwargs["future_points"] == 100
    assert sample_kwargs["min_track_points"] == 171
    assert sample_kwargs["has_fpl"] is True
    assert sample_kwargs["has_scat_prediction"] is True
    assert sample_kwargs["aircraft_type"] == "B738"
    assert result["operation"] == "sample_comparison_model_flights"
    assert result["query"]["eligibility_profile"] == "gbar-align-v1-random-selection"
    assert result["data"]["eligible_count"] == 64


@pytest.mark.asyncio
async def test_random_comparison_sample_rejects_incompatible_maximum_points():
    service = ScatFlightService(repository=Mock())

    with pytest.raises(ValueError, match="至少 171"):
        await service.sample_comparison_model_flights(
            batch="batch",
            sample_size=3,
            max_track_points=170,
        )


@pytest.mark.asyncio
async def test_batch_overview_combines_visualization_and_quality_queries():
    repository = Mock()
    repository.get_batch_statistics = AsyncMock(return_value={"flight_count": 2})
    repository.get_aircraft_type_distribution = AsyncMock(return_value=[])
    repository.get_route_distribution = AsyncMock(return_value=[])
    repository.get_airport_distributions = AsyncMock(
        return_value={
            "departures": [{"airport": "EKCH", "flight_count": 2}],
            "arrivals": [{"airport": "ESSA", "flight_count": 2}],
        }
    )
    repository.get_flight_volume = AsyncMock(
        return_value=[{"period_start": "2016-10-20T00:00:00Z", "flight_count": 2}]
    )
    repository.get_batch_data_quality = AsyncMock(return_value={"missing_callsign_count": 0})
    service = ScatFlightService(repository=repository)

    result = await service.get_batch_overview(
        batch=" batch ", distribution_limit=8, time_granularity="hour"
    )

    repository.get_flight_volume.assert_awaited_once_with(batch="batch", granularity="hour")
    repository.get_batch_data_quality.assert_awaited_once_with(batch="batch")
    repository.get_airport_distributions.assert_awaited_once_with(batch="batch", limit=8)
    assert result["data"]["top_departure_airports"][0]["airport"] == "EKCH"
    assert result["data"]["top_arrival_airports"][0]["airport"] == "ESSA"
    assert result["data"]["flight_volume"][0]["flight_count"] == 2
    assert result["data"]["data_quality"]["missing_callsign_count"] == 0


@pytest.mark.asyncio
async def test_model_data_quality_summary_allows_full_collection_or_batch():
    repository = Mock()
    repository.get_model_data_quality_summary = AsyncMock(
        return_value={"summary": {"flight_count": 13_138}}
    )
    service = ScatFlightService(repository=repository)

    result = await service.get_model_data_quality_summary()
    cached_result = await service.get_model_data_quality_summary()

    repository.get_model_data_quality_summary.assert_awaited_once_with(
        batch=None,
        include_flight_rows=True,
    )
    assert result["query"] == {"batch": None}
    assert result["data"]["summary"]["flight_count"] == 13_138
    assert cached_result == result


@pytest.mark.asyncio
async def test_model_data_quality_clean_flights_are_paginated_and_cached():
    repository = Mock()
    repository.get_model_data_quality_summary = AsyncMock(
        return_value={
            "trajectory_ordering": "stored_array_order_monotonic_triples_only",
            "clean_definition": "zero quality hits",
            "identifier_note": "batch + external_id",
            "_quality_flight_rows": [
                {
                    "external_id": "100002",
                    "original_filename": "100002.json",
                    "batch": "batch-a",
                    "point_count": 10,
                    "missing_point_count": 0,
                    "outlier_point_count": 0,
                    "spike_value_count": 0,
                }
            ],
        }
    )
    service = ScatFlightService(repository=repository)

    result = await service.list_model_data_quality_clean_flights(limit=5)
    cached_result = await service.list_model_data_quality_clean_flights(limit=5)

    repository.get_model_data_quality_summary.assert_awaited_once_with(
        batch=None,
        include_flight_rows=True,
    )
    assert result["data"]["total"] == 1
    assert cached_result == result


@pytest.mark.asyncio
async def test_quality_summary_and_missing_list_share_one_snapshot_scan():
    repository = Mock()
    repository.get_model_data_quality_summary = AsyncMock(
        return_value={
            "summary": {"flight_count": 2, "flights_with_missing": 1},
            "trajectory_ordering": "stored_array_order_monotonic_triples_only",
            "_quality_flight_rows": [
                {
                    "external_id": "100001",
                    "original_filename": "100001.json",
                    "batch": "batch-a",
                    "callsign": "TEST1",
                    "point_count": 10,
                    "missing_point_count": 2,
                    "outlier_point_count": 0,
                    "spike_value_count": 0,
                },
                {
                    "external_id": "100002",
                    "original_filename": "100002.json",
                    "batch": "batch-a",
                    "callsign": "TEST2",
                    "point_count": 10,
                    "missing_point_count": 0,
                    "outlier_point_count": 1,
                    "spike_value_count": 0,
                },
            ],
        }
    )
    service = ScatFlightService(repository=repository)

    summary = await service.get_model_data_quality_summary()
    missing = await service.list_model_data_quality_problem_flights(
        problem_type="missing",
        sort_by="missing_point_count",
    )

    repository.get_model_data_quality_summary.assert_awaited_once_with(
        batch=None,
        include_flight_rows=True,
    )
    assert "_quality_flight_rows" not in summary["data"]
    assert missing["data"]["total"] == 1
    assert missing["data"]["items"][0]["original_filename"] == "100001.json"
    assert missing["data"]["cache_scope"] == "shared_quality_snapshot"


@pytest.mark.asyncio
async def test_search_flights_uses_same_filter_for_count_and_page():
    repository = Mock()
    repository.count_flights = AsyncMock(return_value=7)
    repository.search_flights = AsyncMock(return_value=[])
    service = ScatFlightService(repository=repository)

    result = await service.search_flights(
        batch=" batch ",
        callsign=" sas406 ",
        start_time="2016-10-20T18:00:00+02:00",
        end_time="2016-10-20T20:00:00+02:00",
        sort_by="callsign",
        sort_order="desc",
        limit=2,
        skip=4,
    )

    filters = repository.count_flights.await_args.kwargs
    assert filters["batch"] == "batch"
    assert filters["callsign"] == "SAS406"
    assert filters["start_time"] == datetime(2016, 10, 20, 16, tzinfo=UTC)
    assert filters["end_time"] == datetime(2016, 10, 20, 18, tzinfo=UTC)
    search_kwargs = dict(repository.search_flights.await_args.kwargs)
    assert search_kwargs.pop("sort_by") == "callsign"
    assert search_kwargs.pop("sort_order") == "desc"
    assert search_kwargs.pop("limit") == 2
    assert search_kwargs.pop("skip") == 4
    assert search_kwargs == filters
    assert result["data"] == {"count": 0, "total": 7, "has_more": True, "items": []}


@pytest.mark.asyncio
async def test_track_points_normalizes_time_and_samples_source_indices():
    repository = Mock()
    repository.get_track_points = AsyncMock(
        return_value={
            "id": 1,
            "source": {"batch": "batch", "external_id": "1"},
            "identity": {},
            "start": 10,
            "total_points": 30,
            "plots": [{"time_of_track": f"2016-10-20T20:00:0{index}"} for index in range(5)],
        }
    )
    service = ScatFlightService(repository=repository)

    result = await service.get_track_points(
        batch="batch",
        external_id="1",
        start=10,
        limit=3,
        start_time="2016-10-20T20:00:00+02:00",
        end_time="2016-10-20T21:00:00+02:00",
        every_nth=2,
        fields={"position"},
    )

    repository.get_track_points.assert_awaited_once_with(
        batch="batch",
        external_id="1",
        start=10,
        limit=3,
        start_time="2016-10-20T18:00:00",
        end_time="2016-10-20T19:00:00",
        every_nth=2,
    )
    assert [point["sequence"] for point in result["data"]["points"]] == [10, 12, 14]
    assert all(
        set(point) == {"sequence", "timestamp", "position"} for point in result["data"]["points"]
    )
    assert result["data"]["pagination"] == {
        "start": 10,
        "returned_points": 3,
        "source_points_consumed": 5,
        "total_points": 30,
        "next_start": 15,
        "has_more": True,
    }


@pytest.mark.asyncio
async def test_track_points_rejects_reversed_time_window_before_query():
    repository = Mock()
    repository.get_track_points = AsyncMock()
    service = ScatFlightService(repository=repository)

    with pytest.raises(ValueError, match="start_time"):
        await service.get_track_points(
            batch="batch",
            external_id="1",
            start_time="2016-10-20T21:00:00Z",
            end_time="2016-10-20T20:00:00Z",
        )

    repository.get_track_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_prediction_snapshot_page_contains_metadata_only():
    repository = Mock()
    repository.list_prediction_snapshots = AsyncMock(
        return_value={
            "id": 1,
            "source": {"batch": "batch", "external_id": "1"},
            "identity": {},
            "total_snapshots": 4,
            "snapshots": [
                {"time_stamp": "2016-10-20T20:00:00", "route_point_count": 12},
                {"time_stamp": "2016-10-20T20:05:00", "route_point_count": 13},
            ],
        }
    )
    service = ScatFlightService(repository=repository)

    result = await service.list_prediction_snapshots(
        batch="batch",
        external_id="1",
        start=1,
        limit=2,
    )

    assert result["data"]["pagination"] == {
        "start": 1,
        "returned_snapshots": 2,
        "total_snapshots": 4,
        "has_more": True,
    }
    assert result["data"]["snapshots"][0] == {
        "time": "2016-10-20T20:00:00",
        "route_point_count": 12,
    }
    assert "route" not in result["data"]["snapshots"][0]


@pytest.mark.asyncio
async def test_airport_stats_aggregates_delay_metrics():
    repository = Mock()
    repository.get_airport_statistics = AsyncMock(
        return_value=[
            {
                "airport": "EKCH",
                "flight_count": 3,
                "with_atd_count": 2,
                "mean_delay_minutes": 20.0,
                "max_delay_minutes": 30.0,
                "min_delay_minutes": 10.0,
                "delayed_count": 1,
            },
            {
                "airport": "ESSA",
                "flight_count": 1,
                "with_atd_count": 0,
                "mean_delay_minutes": None,
                "max_delay_minutes": None,
                "min_delay_minutes": None,
                "delayed_count": 0,
            },
        ]
    )
    service = ScatFlightService(repository=repository)

    result = await service.get_airport_stats(batch="scat20161015_20161021", limit=10)

    repository.get_airport_statistics.assert_awaited_once_with(
        batch="scat20161015_20161021", limit=10
    )
    statistics = result["data"]["statistics"]
    assert statistics["flight_count"] == 4
    assert statistics["with_atd_count"] == 2
    assert statistics["mean_delay_minutes"] == 20.0
    assert statistics["delayed_count"] == 1
    assert statistics["delayed_rate"] == 0.5
    assert result["data"]["airports"][0]["airport"] == "EKCH"


@pytest.mark.asyncio
async def test_airport_stats_not_found_for_missing_batch():
    repository = Mock()
    repository.get_airport_statistics = AsyncMock(return_value=None)
    service = ScatFlightService(repository=repository)

    result = await service.get_airport_stats(batch="nope")

    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_flight_phase_stats_formats_phase_rows():
    repository = Mock()
    repository.get_trajectory_phase_stats = AsyncMock(
        return_value=[
            {
                "_id": 1,
                "point_count": 4,
                "start_time": "2016-10-20T11:40:02",
                "end_time": "2016-10-20T11:42:02",
                "min_flight_level": 140.0,
                "max_flight_level": 350.0,
                "average_flight_level": 250.0,
                "average_ground_speed_mps": 200.0,
                "maximum_ground_speed_mps": 220.0,
                "average_abs_rocd_fpm": 1500.0,
                "maximum_abs_rocd_fpm": 2500.0,
            },
            {
                "_id": 0,
                "point_count": 6,
                "start_time": "2016-10-20T11:42:03",
                "end_time": "2016-10-20T11:47:03",
                "min_flight_level": 350.0,
                "max_flight_level": 350.0,
                "average_flight_level": 350.0,
                "average_ground_speed_mps": 210.0,
                "maximum_ground_speed_mps": 215.0,
                "average_abs_rocd_fpm": 50.0,
                "maximum_abs_rocd_fpm": 100.0,
            },
        ]
    )
    service = ScatFlightService(repository=repository)

    result = await service.get_flight_phase_stats(batch="scat20161015_20161021", external_id=100016)

    data = result["data"]
    assert data["total_points"] == 10
    assert [phase["phase_name"] for phase in data["phases"]] == ["climb", "level"]
    climb = data["phases"][0]
    assert climb["duration_seconds"] == 120
    assert climb["point_ratio"] == 0.4
    assert climb["flight_level"]["maximum"] == 350.0


@pytest.mark.asyncio
async def test_prediction_error_stats_matches_fixes_to_track():
    repository = Mock()
    repository.get_latest_prediction_snapshot = AsyncMock(
        return_value={
            "last_snapshot": {
                "time_stamp": "2016-10-20T11:30:00",
                "route": [
                    {
                        "fix_name": "Z9034",
                        "fix_kind": "RP",
                        "eto": "2016-10-20T11:33:59",
                        "lat": 55.61832948785555,
                        "lon": 12.656652833484982,
                        "afl_value": 2.0,
                    },
                    {
                        "fix_name": "LATER",
                        "fix_kind": "RP",
                        "eto": "2016-10-20T13:00:00",
                        "lat": 56.0,
                        "lon": 13.0,
                        "afl_value": 350.0,
                    },
                ],
            }
        }
    )
    repository.get_track_arrays_for_error_stats = AsyncMock(
        return_value=[
            {
                "timestamp": "2016-10-20T11:34:00",
                "lat": 55.618,  # 与预测点不同，产生水平误差
                "lon": 12.656,
                "flight_level": 2.5,
            },
            {
                "timestamp": "2016-10-20T11:40:00",
                "lat": 55.62,
                "lon": 12.66,
                "flight_level": 100.0,
            },
        ]
    )
    service = ScatFlightService(repository=repository)

    result = await service.get_prediction_error_stats(
        batch="scat20161015_20161021",
        external_id=100016,
    )

    data = result["data"]
    assert data["matched_fix_count"] == 1
    assert data["unmatched_fix_count"] == 1
    fixes = data["fixes"]
    assert fixes[0]["fix_name"] == "Z9034"
    assert fixes[0]["time_error_s"] == 1.0
    assert fixes[0]["horizontal_error_m"] > 0
    assert fixes[0]["vertical_error_m"] == pytest.approx(0.5 * 30.48, abs=0.2)
    statistics = data["statistics"]
    assert statistics["horizontal_error"]["rmse_m"] > 0
    assert statistics["vertical_error"]["mean_abs_m"] is not None
    assert statistics["time_error"]["mean_abs_s"] == 1.0


@pytest.mark.asyncio
async def test_prediction_error_stats_not_found_without_snapshot():
    repository = Mock()
    repository.get_latest_prediction_snapshot = AsyncMock(return_value=None)
    repository.get_track_arrays_for_error_stats = AsyncMock(return_value=[])
    service = ScatFlightService(repository=repository)

    result = await service.get_prediction_error_stats(
        batch="scat20161015_20161021", external_id=100016
    )

    assert result["status"] == "not_found"
