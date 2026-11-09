"""M6-09 API 测试：自研模型实验读取（评估页对比数据）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from core import settings
from service import app
from service.experiments_api import get_experiment_repository

BATCH = "scat20161015_20161021"

EXPERIMENT = {
    "experiment_id": "m6eval-kalman-cv-scat20161015_20161021-20260920T075645-acce1d",
    "kind": "predictor_evaluation",
    "status": "succeeded",
    "batch": BATCH,
    "predictor_id": "kalman-cv",
    "predictor_version": "1",
    "data_version": "scat-import-2",
    "protocol_version": "m6-eval-v1",
    "source": "chat",
    "created_at": "2026-09-20T07:56:45Z",
    "finished_at": "2026-09-20T09:00:58Z",
    "metrics": [
        {"horizon_s": 60.0, "sample_count": 2052, "flight_count": 200, "mae_m": 463.3},
    ],
}


def _auth_headers() -> dict[str, str]:
    if settings.AUTH_SECRET:
        return {"Authorization": f"Bearer {settings.AUTH_SECRET.get_secret_value()}"}
    return {}


@pytest.fixture
def experiments_api():
    repository = Mock()
    repository.list_with_metrics = AsyncMock(return_value=[EXPERIMENT])
    app.dependency_overrides[get_experiment_repository] = lambda: repository
    client = TestClient(app, raise_server_exceptions=False, headers=_auth_headers())

    yield client, repository

    client.close()
    app.dependency_overrides.pop(get_experiment_repository, None)


def test_lists_model_experiments_with_batch_and_limit(experiments_api):
    client, repository = experiments_api

    response = client.get(
        "/api/v1/scat/experiments/model-experiments",
        params={"batch": BATCH, "limit": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["batch"] == BATCH
    assert payload["count"] == 1
    assert payload["experiments"][0]["experiment_id"] == EXPERIMENT["experiment_id"]
    assert payload["experiments"][0]["metrics"][0]["mae_m"] == 463.3
    await_args = repository.list_with_metrics.await_args
    assert await_args is not None
    assert await_args.kwargs == {"batch": BATCH, "predictor_id": None, "limit": 5}


def test_filters_by_predictor_id(experiments_api):
    client, repository = experiments_api

    response = client.get(
        "/api/v1/scat/experiments/model-experiments",
        params={"batch": BATCH, "predictor_id": "kalman-cv"},
    )

    assert response.status_code == 200
    assert repository.list_with_metrics.await_args.kwargs["predictor_id"] == "kalman-cv"


def test_rejects_out_of_range_limit(experiments_api):
    client, _repository = experiments_api

    response = client.get(
        "/api/v1/scat/experiments/model-experiments",
        params={"limit": 999},
    )

    assert response.status_code == 422
