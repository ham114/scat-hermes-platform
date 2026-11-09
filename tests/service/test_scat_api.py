from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import AutoReconnect

from core import settings
from service import app
from service.scat_api import get_scat_service


def _auth_headers() -> dict[str, str]:
    if settings.AUTH_SECRET:
        return {"Authorization": f"Bearer {settings.AUTH_SECRET.get_secret_value()}"}
    return {}


@pytest.fixture
def scat_api():
    service = Mock()
    service.list_batches = AsyncMock()
    service.get_batch_overview = AsyncMock()
    service.search_flights = AsyncMock()
    service.get_flight_overview = AsyncMock()
    service.get_track_points = AsyncMock()
    service.get_fpl_overview = AsyncMock()
    service.list_prediction_snapshots = AsyncMock()
    service.get_prediction_snapshot = AsyncMock()
    app.dependency_overrides[get_scat_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False, headers=_auth_headers())

    yield client, service

    client.close()
    app.dependency_overrides.pop(get_scat_service, None)


def _flight() -> dict:
    return {
        "dataset_id": 100016,
        "source": {
            "system": "scat-public-dataset",
            "batch": "scat20161015_20161021",
            "external_id": "100016",
            "external_id_type": "str",
        },
        "identity": {
            "callsign": "SAS406",
            "aircraft_type": "A320",
            "departure_airport": "EKCH",
            "destination_airport": "ESSA",
            "iobt_utc": "2016-10-20T18:45:00Z",
            "atd_utc": None,
            "candidate_key": "v1|SAS406|EKCH|2016-10-20T18:45:00Z",
            "identity_confidence": 1.0,
        },
        "control_centre_transfer_count": 0,
        "control_centre_history": [],
        "ingest": {"original_filename": "sample.json", "uploaded_at": None},
    }


def _fpl() -> dict:
    return {
        "event_counts": {
            "base": 1,
            "departure": 1,
            "arrival": 1,
            "clearance": 0,
            "holding": 0,
            "plan_update": 1,
        },
        "departure": {
            "runway": None,
            "sid": None,
            "iobt": None,
            "atd": None,
            "last_update_time": None,
        },
        "arrival": {
            "runway": None,
            "star": None,
            "approach_clearance": False,
            "missed_approach": False,
            "last_update_time": None,
        },
        "latest_clearance": {
            "cleared_level": None,
            "cleared_level_unit": None,
            "assigned_speed": None,
            "assigned_speed_unit": None,
            "assigned_heading": None,
            "assigned_heading_beacon": None,
            "last_update_time": None,
        },
        "holding": {
            "had_holding": False,
            "latest_status": None,
            "latest_stack_status": None,
            "entry_time": None,
            "leaving_time": None,
        },
        "route_plan": {
            "icao_route": None,
            "requested_flight_level": None,
            "true_airspeed": None,
            "entry_point": None,
            "exit_point": None,
            "last_update_time": None,
        },
        "had_approach_clearance": False,
    }


def test_list_batches_returns_explicit_pagination(scat_api):
    client, service = scat_api
    service.list_batches.return_value = {
        "status": "ok",
        "data": {
            "count": 1,
            "total": 3,
            "has_more": True,
            "items": [
                {
                    "batch": "scat20161015_20161021",
                    "flight_count": 42,
                    "earliest_iobt_utc": "2016-10-15T00:00:00Z",
                    "latest_iobt_utc": "2016-10-21T23:59:59Z",
                    "earliest_upload_time": None,
                    "latest_upload_time": None,
                }
            ],
        },
    }

    response = client.get("/api/v1/scat/batches?limit=1&offset=1")

    assert response.status_code == 200
    assert response.json()["pagination"] == {
        "offset": 1,
        "limit": 1,
        "returned": 1,
        "total": 3,
        "has_more": True,
    }
    service.list_batches.assert_awaited_once_with(limit=1, skip=1)


def test_batch_overview_maps_service_not_found_to_safe_404(scat_api):
    client, service = scat_api
    service.get_batch_overview.return_value = {
        "status": "not_found",
        "message": "没有找到批次 missing",
        "data": None,
    }

    response = client.get(
        "/api/v1/scat/batches/missing/overview",
        headers={"X-Request-ID": "m2-not-found"},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "m2-not-found"
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "没有找到批次 missing",
            "request_id": "m2-not-found",
        }
    }


def test_batch_overview_returns_visualization_data_and_forwards_granularity(scat_api):
    client, service = scat_api
    service.get_batch_overview.return_value = {
        "status": "ok",
        "data": {
            "statistics": {
                "batch": "batch",
                "flight_count": 10,
                "unique_callsign_count": 9,
                "aircraft_type_count": 3,
                "departure_airport_count": 4,
                "arrival_airport_count": 5,
                "total_track_points": 1000,
                "average_track_points": 100,
                "minimum_track_points": 80,
                "maximum_track_points": 120,
                "flights_with_prediction": 8,
                "total_prediction_snapshots": 40,
            },
            "top_aircraft_types": [{"aircraft_type": "A320", "flight_count": 6}],
            "top_routes": [{"adep": "ZBAA", "ades": "ZSSS", "flight_count": 4}],
            "top_departure_airports": [{"airport": "ZBAA", "flight_count": 5}],
            "top_arrival_airports": [{"airport": "ZSSS", "flight_count": 5}],
            "flight_volume": [{"period_start": "2016-10-20T18:00:00Z", "flight_count": 3}],
            "data_quality": {
                "missing_callsign_count": 0,
                "missing_aircraft_type_count": 1,
                "missing_departure_airport_count": 0,
                "missing_arrival_airport_count": 0,
                "missing_iobt_count": 0,
                "empty_trajectory_count": 0,
                "flights_without_prediction_count": 2,
            },
        },
    }

    response = client.get(
        "/api/v1/scat/batches/batch/overview",
        params={"distribution_limit": 12, "time_granularity": "hour"},
    )

    assert response.status_code == 200
    assert response.json()["top_departure_airports"][0]["airport"] == "ZBAA"
    assert response.json()["flight_volume"][0]["flight_count"] == 3
    assert response.json()["data_quality"]["missing_aircraft_type_count"] == 1
    # M4-09：默认请求物化优先（auto），由服务决定物化结果或实时回退
    assert response.json()["statistics_source"] == "live"
    service.get_batch_overview.assert_awaited_once_with(
        batch="batch",
        distribution_limit=12,
        time_granularity="hour",
        statistics_source="auto",
    )


def test_batch_overview_forwards_explicit_live_statistics_source(scat_api):
    client, service = scat_api
    service.get_batch_overview.return_value = {
        "status": "ok",
        "data": {
            "statistics": {
                "batch": "batch",
                "flight_count": 10,
                "unique_callsign_count": 9,
                "aircraft_type_count": 3,
                "departure_airport_count": 4,
                "arrival_airport_count": 5,
                "total_track_points": 1000,
                "average_track_points": 100,
                "minimum_track_points": 80,
                "maximum_track_points": 120,
                "flights_with_prediction": 8,
                "total_prediction_snapshots": 40,
            },
            "top_aircraft_types": [],
            "top_routes": [],
            "top_departure_airports": [],
            "top_arrival_airports": [],
            "flight_volume": [],
            "data_quality": {
                "missing_callsign_count": 0,
                "missing_aircraft_type_count": 1,
                "missing_departure_airport_count": 0,
                "missing_arrival_airport_count": 0,
                "missing_iobt_count": 0,
                "empty_trajectory_count": 0,
                "flights_without_prediction_count": 2,
            },
            "statistics_source": "materialized",
            "statistics_generated_at": "2026-09-17T12:00:00Z",
        },
    }

    response = client.get(
        "/api/v1/scat/batches/batch/overview",
        params={"statistics_source": "live"},
    )

    assert response.status_code == 200
    assert response.json()["statistics_source"] == "materialized"
    assert response.json()["statistics_generated_at"] == "2026-09-17T12:00:00Z"
    service.get_batch_overview.assert_awaited_once_with(
        batch="batch",
        distribution_limit=10,
        time_granularity="day",
        statistics_source="live",
    )


def test_batch_overview_rejects_unknown_statistics_source(scat_api):
    client, _ = scat_api

    response = client.get(
        "/api/v1/scat/batches/batch/overview",
        params={"statistics_source": "guess"},
    )

    assert response.status_code == 422


def test_search_flights_returns_total_and_forwards_filters(scat_api):
    client, service = scat_api
    service.search_flights.return_value = {
        "status": "ok",
        "data": {"count": 1, "total": 6, "has_more": True, "items": [_flight()]},
    }

    response = client.get(
        "/api/v1/scat/flights",
        params={
            "batch": "scat20161015_20161021",
            "callsign": "sas406",
            "start_time": "2016-10-20T18:00:00Z",
            "end_time": "2016-10-21T18:00:00Z",
            "sort_by": "callsign",
            "sort_order": "desc",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 6
    assert response.json()["items"][0]["identity"]["callsign"] == "SAS406"
    service.search_flights.assert_awaited_once_with(
        batch="scat20161015_20161021",
        callsign="sas406",
        aircraft_type=None,
        adep=None,
        ades=None,
        candidate_key=None,
        start_time=datetime(2016, 10, 20, 18, tzinfo=UTC),
        end_time=datetime(2016, 10, 21, 18, tzinfo=UTC),
        sort_by="callsign",
        sort_order="desc",
        limit=1,
        skip=0,
    )


def test_empty_flight_page_is_a_successful_bounded_result(scat_api):
    client, service = scat_api
    service.search_flights.return_value = {
        "status": "ok",
        "data": {"count": 0, "total": 0, "has_more": False, "items": []},
    }

    response = client.get("/api/v1/scat/flights?batch=empty")

    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 0
    assert response.json()["items"] == []


@pytest.mark.parametrize(
    ("path", "parameter"),
    [
        ("/api/v1/scat/flights?limit=101", "limit"),
        ("/api/v1/scat/flights?offset=-1", "offset"),
        ("/api/v1/scat/flights/batch/id/trajectory?limit=501", "limit"),
        ("/api/v1/scat/flights/batch/id/predictions?limit=101", "limit"),
    ],
)
def test_list_bounds_are_rejected(scat_api, path, parameter):
    client, _ = scat_api

    response = client.get(path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert any(
        detail["location"][-1] == parameter for detail in response.json()["error"]["details"]
    )


def test_path_identifiers_are_strictly_validated(scat_api):
    client, service = scat_api

    response = client.get("/api/v1/scat/flights/not%20valid/100016")

    assert response.status_code == 422
    service.get_flight_overview.assert_not_awaited()


def test_trajectory_forwards_time_window_downsampling_and_field_selection(scat_api):
    client, service = scat_api
    service.get_track_points.return_value = {
        "status": "ok",
        "data": {
            "flight": _flight(),
            "pagination": {
                "start": 10,
                "returned_points": 1,
                "source_points_consumed": 5,
                "total_points": 30,
                "next_start": 15,
                "has_more": True,
            },
            "points": [
                {
                    "sequence": 10,
                    "timestamp": "2016-10-20T20:00:00Z",
                    "position": {
                        "lat_deg": 55.0,
                        "lon_deg": 12.0,
                        "flight_level": 200.0,
                        "altitude_ft": 20000.0,
                        "altitude_m": 6096.0,
                    },
                }
            ],
        },
    }

    response = client.get(
        "/api/v1/scat/flights/batch/100016/trajectory",
        params={
            "start": 10,
            "limit": 1,
            "start_time": "2016-10-20T20:00:00+00:00",
            "end_time": "2016-10-20T21:00:00+00:00",
            "every_nth": 5,
            "fields": "position",
        },
    )

    assert response.status_code == 200
    assert response.json()["pagination"]["next_start"] == 15
    assert set(response.json()["points"][0]) == {"sequence", "timestamp", "position"}
    service.get_track_points.assert_awaited_once_with(
        batch="batch",
        external_id="100016",
        start=10,
        limit=1,
        start_time="2016-10-20T20:00:00+00:00",
        end_time="2016-10-20T21:00:00+00:00",
        every_nth=5,
        fields={"position"},
    )


def test_prediction_list_does_not_return_route_arrays(scat_api):
    client, service = scat_api
    service.list_prediction_snapshots.return_value = {
        "status": "ok",
        "data": {
            "flight": _flight(),
            "pagination": {
                "start": 0,
                "returned_snapshots": 1,
                "total_snapshots": 2,
                "has_more": True,
            },
            "snapshots": [{"time": "2016-10-20T21:00:00Z", "route_point_count": 12}],
        },
    }

    response = client.get("/api/v1/scat/flights/batch/100016/predictions?limit=1")

    assert response.status_code == 200
    assert response.json()["snapshots"][0] == {
        "time": "2016-10-20T21:00:00Z",
        "route_point_count": 12,
    }
    assert "route" not in response.json()["snapshots"][0]


def test_prediction_snapshot_requires_timezone(scat_api):
    client, service = scat_api
    timestamp = quote("2016-10-20T21:00:00", safe="")

    response = client.get(f"/api/v1/scat/flights/batch/100016/prediction-snapshots/{timestamp}")

    assert response.status_code == 422
    assert "timezone" in response.json()["error"]["message"]
    service.get_prediction_snapshot.assert_not_awaited()


def test_database_error_is_sanitized(scat_api):
    client, service = scat_api
    service.list_batches.side_effect = RuntimeError("mongodb://user:secret@database")

    response = client.get(
        "/api/v1/scat/batches",
        headers={"X-Request-ID": "m2-database-error"},
    )

    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "Internal server error",
        "request_id": "m2-database-error",
    }


def test_mongodb_error_is_sanitized_as_service_unavailable(scat_api):
    client, service = scat_api
    service.list_batches.side_effect = AutoReconnect("mongodb://user:secret@database")

    response = client.get(
        "/api/v1/scat/batches",
        headers={"X-Request-ID": "m2-mongo-error"},
    )

    assert response.status_code == 503
    assert "secret" not in response.text
    assert response.json()["error"] == {
        "code": "database_unavailable",
        "message": "SCAT database is temporarily unavailable",
        "request_id": "m2-mongo-error",
    }


def test_scat_routes_follow_global_auth_setting(scat_api, monkeypatch):
    client, service = scat_api
    service.list_batches.return_value = {
        "status": "ok",
        "data": {"count": 0, "total": 0, "has_more": False, "items": []},
    }
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("m2-secret"))

    denied = client.get(
        "/api/v1/scat/batches",
        headers={"Authorization": "Bearer wrong"},
    )
    allowed = client.get(
        "/api/v1/scat/batches",
        headers={"Authorization": "Bearer m2-secret"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert service.list_batches.await_count == 1

    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    disabled = client.get(
        "/api/v1/scat/batches",
        headers={"Authorization": "Bearer wrong"},
    )
    assert disabled.status_code == 200


def test_openapi_contains_all_initial_scat_paths(scat_api):
    client, _ = scat_api

    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    assert "/api/v1/scat/batches" in paths
    assert "/api/v1/scat/batches/{batch}/overview" in paths
    assert "/api/v1/scat/flights" in paths
    assert "/api/v1/scat/flights/{batch}/{external_id}" in paths
    assert "/api/v1/scat/flights/{batch}/{external_id}/trajectory" in paths
    assert "/api/v1/scat/flights/{batch}/{external_id}/fpl" in paths
    assert "/api/v1/scat/flights/{batch}/{external_id}/predictions" in paths
    assert (
        "/api/v1/scat/flights/{batch}/{external_id}/prediction-snapshots/{snapshot_time}" in paths
    )


def test_get_airport_stats(scat_api):
    client, service = scat_api
    service.get_airport_stats = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "statistics": {
                    "flight_count": 2,
                    "airport_count": 1,
                    "with_atd_count": 2,
                    "mean_delay_minutes": 10.0,
                    "delayed_count": 0,
                    "delayed_rate": 0.0,
                    "delay_threshold_minutes": 15,
                },
                "airports": [
                    {
                        "airport": "EKCH",
                        "flight_count": 2,
                        "with_atd_count": 2,
                        "mean_delay_minutes": 10.0,
                        "max_delay_minutes": 12.0,
                        "min_delay_minutes": 8.0,
                        "delayed_count": 0,
                        "delayed_rate": 0.0,
                    }
                ],
            },
        }
    )

    response = client.get(
        "/api/v1/scat/batches/scat20161015_20161021/airports?limit=10"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["statistics"]["flight_count"] == 2
    assert payload["airports"][0]["airport"] == "EKCH"
    service.get_airport_stats.assert_awaited_once_with(
        batch="scat20161015_20161021", limit=10
    )


def test_get_flight_phases(scat_api):
    client, service = scat_api
    service.get_flight_phase_stats = AsyncMock(
        return_value={
            "status": "ok",
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
                        "min_flight_level": 140.0,
                        "max_flight_level": 350.0,
                        "average_flight_level": 250.0,
                        "average_ground_speed_mps": 200.0,
                        "maximum_ground_speed_mps": 220.0,
                        "average_abs_rocd_fpm": 1500.0,
                        "maximum_abs_rocd_fpm": 2500.0,
                    }
                ],
            },
        }
    )

    response = client.get(
        "/api/v1/scat/flights/scat20161015_20161021/100016/phases"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phases"][0]["phase_name"] == "climb"
    service.get_flight_phase_stats.assert_awaited_once_with(
        batch="scat20161015_20161021", external_id="100016"
    )


def test_get_prediction_error_stats(scat_api):
    client, service = scat_api
    service.get_prediction_error_stats = AsyncMock(
        return_value={
            "status": "ok",
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

    response = client.get(
        "/api/v1/scat/flights/scat20161015_20161021/100016/prediction-error"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched_fix_count"] == 1
    assert payload["fixes"][0]["horizontal_error_m"] == 320.5
    service.get_prediction_error_stats.assert_awaited_once_with(
        batch="scat20161015_20161021",
        external_id="100016",
        timestamp=None,
        match_window_seconds=300.0,
    )
