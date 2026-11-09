"""API tests for the deterministic UAV REST endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from core import settings
from service import app
from service.uav_api import get_uav_service


def _auth_headers() -> dict[str, str]:
    if settings.AUTH_SECRET:
        return {"Authorization": f"Bearer {settings.AUTH_SECRET.get_secret_value()}"}
    return {}


@pytest.fixture
def uav_api():
    service = Mock()
    service.list_models = AsyncMock()
    service.list_flights = AsyncMock()
    service.get_flight = AsyncMock()
    service.get_samples = AsyncMock()
    service.list_channels = Mock()  # 同步方法
    app.dependency_overrides[get_uav_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False, headers=_auth_headers())

    yield client, service

    client.close()
    app.dependency_overrides.pop(get_uav_service, None)


def _flight_page_item() -> dict:
    return {
        "external_id": "20230330122740_DTC_53.1394B",
        "model": "0001",
        "tail_number": None,
        "mission": "DTC",
        "flight_seq": 53,
        "takeoff_at": "2023-03-30T04:27:40Z",
        "duration_s": 4505.0,
        "sample_count": 4506,
        "sample_rate_hz": 1.0,
        "flight_kind": "normal",
        "label": None,
    }


def test_list_models(uav_api):
    client, service = uav_api
    service.list_models.return_value = {
        "status": "ok",
        "data": {
            "pagination": {
                "offset": 0, "limit": 1, "returned": 1, "total": 1, "has_more": False,
            },
            "items": [
                {
                    "model": "0001",
                    "flight_count": 14,
                    "earliest_takeoff_utc": "2022-07-23T04:40:28Z",
                    "latest_takeoff_utc": None,
                }
            ],
        },
    }

    response = client.get("/api/v1/uav/models")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["model"] == "0001"


def test_search_flights_returns_page(uav_api):
    client, service = uav_api
    service.list_flights.return_value = {
        "status": "ok",
        "data": {
            "pagination": {
                "offset": 0, "limit": 20, "returned": 1, "total": 1, "has_more": False,
            },
            "items": [_flight_page_item()],
        },
    }

    response = client.get("/api/v1/uav/flights?model=0001&sort_by=takeoff_at&sort_order=1")

    assert response.status_code == 200
    assert response.json()["items"][0]["external_id"] == "20230330122740_DTC_53.1394B"
    service.list_flights.assert_awaited_once()


def test_search_flights_rejects_unknown_sort(uav_api):
    client, _ = uav_api

    response = client.get("/api/v1/uav/flights?sort_by=bogus")

    assert response.status_code == 422


def test_search_flights_rejects_naive_takeoff_time(uav_api):
    client, _ = uav_api

    response = client.get("/api/v1/uav/flights?takeoff_start=2023-03-30T04:27:40")

    assert response.status_code == 422


def test_get_flight_detail(uav_api):
    client, service = uav_api
    service.get_flight.return_value = {
        "status": "ok",
        "data": {
            "external_id": "20230330122740_DTC_53.1394B",
            "model": "0001",
            "tail_number": None,
            "mission": "DTC",
            "flight_seq": 53,
            "takeoff_at": "2023-03-30T04:27:40Z",
            "duration_s": 4505.0,
            "sample_count": 4506,
            "sample_rate_hz": 1.0,
            "flight_kind": "normal",
            "label": None,
            "platform": "fixed-wing-uav",
            "original_filename": "20230330122740_DTC_53.1394B.xlsx",
            "uploaded_at": "2026-09-12T00:00:00Z",
            "header_layout_note": None,
            "extra_column_count": 0,
            "summary": {
                "altitude_m": {"min": 9.0, "max": 800.0},
                "ground_speed_mps": {"min": 0.0, "max": 118.0},
                "position_bounds": {
                    "min_lat": 28.0, "max_lat": 29.0, "min_lon": 115.0, "max_lon": 117.0,
                },
            },
            "channel_stats": [
                {"key": "cmp_comb_alt", "min": 9.0, "max": 800.0, "avg": 400.0, "missing": 0}
            ],
        },
    }

    response = client.get("/api/v1/uav/flights/20230330122740_DTC_53.1394B")

    assert response.status_code == 200
    assert response.json()["summary"]["altitude_m"]["max"] == 800.0


def test_get_flight_not_found(uav_api):
    client, service = uav_api
    service.get_flight.return_value = {
        "status": "not_found",
        "message": "UAV flight 'nope' was not found",
    }

    response = client.get("/api/v1/uav/flights/nope")

    assert response.status_code == 404


def test_get_samples_page(uav_api):
    client, service = uav_api
    service.get_samples.return_value = {
        "status": "ok",
        "data": {
            "external_id": "20230330122740_DTC_53.1394B",
            "start_time": "2023-03-30T04:27:40Z",
            "start": 0,
            "limit": 200,
            "every_nth": 2,
            "returned": 100,
            "has_more": True,
            "next_start": 200,
            "samples": [{"seq": 0, "t_s": 0.0, "cmp_comb_alt": 10.0}],
        },
    }

    response = client.get(
        "/api/v1/uav/flights/20230330122740_DTC_53.1394B/samples"
        "?start=0&limit=200&every_nth=2&fields=position&channels=cmp_comb_alt"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 100
    assert body["start_time"] == "2023-03-30T04:27:40Z"
    assert body["samples"][0]["cmp_comb_alt"] == 10.0
    kwargs = service.get_samples.await_args.kwargs
    assert kwargs["fields"] == {"position"}
    assert kwargs["channels"] == {"cmp_comb_alt"}


def test_get_samples_rejects_unknown_fields(uav_api):
    client, _ = uav_api

    response = client.get(
        "/api/v1/uav/flights/abc/samples?fields=bogus"
    )

    assert response.status_code == 422


def test_get_samples_rejects_unknown_channel_from_service(uav_api):
    client, service = uav_api
    service.get_samples.side_effect = ValueError("unknown channels: ['bogus']")

    response = client.get(
        "/api/v1/uav/flights/abc/samples?channels=bogus"
    )

    assert response.status_code == 422


def test_get_samples_rejects_invalid_external_id(uav_api):
    client, _ = uav_api

    response = client.get("/api/v1/uav/flights/中文/samples")

    assert response.status_code == 422


def test_list_channels(uav_api):
    client, service = uav_api
    service.list_channels.return_value = {
        "status": "ok",
        "data": {
            "items": [
                {"key": "cmp_comb_alt", "label": "组合高度", "unit": "m", "group": "position"}
            ]
        },
    }

    response = client.get("/api/v1/uav/channels")

    assert response.status_code == 200
    assert response.json()[0]["key"] == "cmp_comb_alt"


def test_get_channel_stats(uav_api):
    client, service = uav_api
    service.get_channel_stats = AsyncMock(
        return_value={
            "status": "ok",
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

    response = client.get(
        "/api/v1/uav/stats/channels?channels=cmp_comb_alt&model=0007"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["channel"] == "cmp_comb_alt"
    service.get_channel_stats.assert_awaited_once_with(
        channels={"cmp_comb_alt"}, model="0007", flight_kind=None
    )


def test_get_channel_stats_rejects_bad_flight_kind(uav_api):
    client, service = uav_api

    response = client.get(
        "/api/v1/uav/stats/channels?channels=cmp_comb_alt&flight_kind=weird"
    )

    assert response.status_code == 422
    service.get_channel_stats.assert_not_called()


def test_get_normal_vs_anomaly(uav_api):
    client, service = uav_api
    service.get_normal_vs_anomaly_stats = AsyncMock(
        return_value={
            "status": "ok",
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

    response = client.get(
        "/api/v1/uav/stats/normal-vs-anomaly?channels=cmp_pitch"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["delta_mean"] == 4.0
    service.get_normal_vs_anomaly_stats.assert_awaited_once_with(
        channels={"cmp_pitch"}, model=None
    )


def test_get_flight_phases(uav_api):
    client, service = uav_api
    service.get_flight_phase_stats = AsyncMock(
        return_value={
            "status": "ok",
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
                        "min_altitude_m": 100.0,
                        "max_altitude_m": 800.0,
                        "average_altitude_m": 450.0,
                        "average_ground_speed_mps": 30.0,
                        "maximum_ground_speed_mps": 35.0,
                        "average_vertical_speed_mps": 3.5,
                    }
                ],
            },
        }
    )

    response = client.get(
        "/api/v1/uav/flights/20230330122740_DTC_53.1394B/phases"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phases"][0]["phase"] == "climb"
    service.get_flight_phase_stats.assert_awaited_once_with(
        "20230330122740_DTC_53.1394B"
    )
