"""M4-09 API 测试：物化统计读取与物化任务查询。"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from core import settings
from service import app
from service.materialization_api import get_materialization_service

BATCH = "scat20161015_20161021"
JOB_ID = "flight_features-scat20161015_20161021-20260917T142004-e8f47d"

STATISTICS = {
    "batch": BATCH,
    "flight_count": 13138,
    "unique_callsign_count": 13000,
    "aircraft_type_count": 42,
    "departure_airport_count": 120,
    "arrival_airport_count": 130,
    "total_track_points": 5500081,
    "average_track_points": 418.6,
    "minimum_track_points": 1,
    "maximum_track_points": 5000,
    "flights_with_prediction": 13136,
    "total_prediction_snapshots": 900000,
}

DATA_QUALITY = {
    "missing_callsign_count": 0,
    "missing_aircraft_type_count": 3,
    "missing_departure_airport_count": 0,
    "missing_arrival_airport_count": 0,
    "missing_iobt_count": 1,
    "empty_trajectory_count": 2,
    "flights_without_prediction_count": 2,
}


def _auth_headers() -> dict[str, str]:
    if settings.AUTH_SECRET:
        return {"Authorization": f"Bearer {settings.AUTH_SECRET.get_secret_value()}"}
    return {}


def _job_document() -> dict:
    return {
        "job_id": JOB_ID,
        "kind": "flight_features",
        "batch": BATCH,
        "status": "partial",
        "progress": 1.0,
        "total_count": 200,
        "processed_count": 200,
        "failed_count": 1,
        "failed_targets": ["100066"],
        "failure_detail": {"100066": "航班无有效轨迹点"},
        "error_message": None,
        "retry_of": None,
        "generated_at": "2026-09-17T14:20:04Z",
        "expires_at": "2026-10-17T14:20:04Z",
    }


@pytest.fixture
def materialization_api():
    service = Mock()
    service.read_batch_statistics = AsyncMock()
    service.list_jobs = AsyncMock()
    service.get_job = AsyncMock()
    app.dependency_overrides[get_materialization_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False, headers=_auth_headers())

    yield client, service

    client.close()
    app.dependency_overrides.pop(get_materialization_service, None)


def test_materialized_statistics_returns_record_and_source(materialization_api):
    client, service = materialization_api
    service.read_batch_statistics.return_value = {
        "status": "ok",
        "query": {"batch": BATCH},
        "data": {
            "batch": BATCH,
            "source": "materialized",
            "statistics": STATISTICS,
            "data_quality": DATA_QUALITY,
            "generated_at": "2026-09-17T14:13:54Z",
            "source_version": "scat-import-2",
            "algorithm_version": "1",
            "fallback_reason": None,
        },
    }

    response = client.get(f"/api/v1/scat/materialization/batches/{BATCH}/statistics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "materialized"
    assert payload["statistics"]["flight_count"] == 13138
    assert payload["algorithm_version"] == "1"
    assert payload["generated_at"] == "2026-09-17T14:13:54Z"
    service.read_batch_statistics.assert_awaited_once_with(
        batch=BATCH,
        statistics_source="auto",
    )


def test_materialized_statistics_reports_live_fallback_reason(materialization_api):
    client, service = materialization_api
    service.read_batch_statistics.return_value = {
        "status": "ok",
        "query": {"batch": BATCH},
        "data": {
            "batch": BATCH,
            "source": "live",
            "statistics": STATISTICS,
            "data_quality": DATA_QUALITY,
            "generated_at": None,
            "source_version": None,
            "algorithm_version": None,
            "fallback_reason": "missing",
        },
    }

    response = client.get(
        f"/api/v1/scat/materialization/batches/{BATCH}/statistics",
        params={"statistics_source": "live"},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "live"
    assert response.json()["fallback_reason"] == "missing"
    service.read_batch_statistics.assert_awaited_once_with(
        batch=BATCH,
        statistics_source="live",
    )


def test_materialized_statistics_rejects_unknown_source(materialization_api):
    client, _ = materialization_api

    response = client.get(
        f"/api/v1/scat/materialization/batches/{BATCH}/statistics",
        params={"statistics_source": "guess"},
    )

    assert response.status_code == 422


def test_materialized_statistics_returns_404_for_unknown_batch(materialization_api):
    client, service = materialization_api
    service.read_batch_statistics.return_value = {
        "status": "not_found",
        "message": "没有找到批次 unknown",
        "query": {"batch": "unknown"},
        "data": None,
    }

    response = client.get("/api/v1/scat/materialization/batches/unknown/statistics")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "not_found"
    assert "unknown" in payload["error"]["message"]


def test_list_jobs_forwards_filters_and_returns_items(materialization_api):
    client, service = materialization_api
    service.list_jobs.return_value = {
        "status": "ok",
        "query": {"kind": "flight_features", "batch": BATCH, "status": "partial"},
        "data": {"items": [_job_document()], "limit": 10, "returned": 1},
    }

    response = client.get(
        "/api/v1/scat/materialization/jobs",
        params={
            "kind": "flight_features",
            "batch": BATCH,
            "status": "partial",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 10
    assert payload["items"][0]["failed_targets"] == ["100066"]
    assert payload["items"][0]["failure_detail"] == {"100066": "航班无有效轨迹点"}
    service.list_jobs.assert_awaited_once_with(
        kind="flight_features",
        batch=BATCH,
        status_filter="partial",
        limit=10,
    )


def test_list_jobs_rejects_unknown_filters_and_oversized_limit(materialization_api):
    client, _ = materialization_api

    assert (
        client.get(
            "/api/v1/scat/materialization/jobs", params={"kind": "guessed_kind"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/scat/materialization/jobs", params={"status": "unknown"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/scat/materialization/jobs", params={"limit": 5000}
        ).status_code
        == 422
    )


def test_get_job_returns_progress_and_failures(materialization_api):
    client, service = materialization_api
    service.get_job.return_value = {
        "status": "ok",
        "query": {"job_id": JOB_ID},
        "data": _job_document(),
    }

    response = client.get(f"/api/v1/scat/materialization/jobs/{JOB_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["progress"] == 1.0
    assert payload["processed_count"] == 200
    service.get_job.assert_awaited_once_with(JOB_ID)


def test_get_job_maps_not_found_and_rejects_short_ids(materialization_api):
    client, service = materialization_api
    service.get_job.return_value = {
        "status": "not_found",
        "message": f"没有找到物化任务 {JOB_ID}",
        "query": {"job_id": JOB_ID},
        "data": None,
    }

    assert client.get(f"/api/v1/scat/materialization/jobs/{JOB_ID}").status_code == 404
    assert client.get("/api/v1/scat/materialization/jobs/short").status_code == 422
