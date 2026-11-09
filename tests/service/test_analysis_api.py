from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from analysis_runs import AnalysisArtifactRecord
from core import settings
from core.settings import AnalysisIdentityMode
from schema.analysis import AnalysisResult, MetricItem, MetricsBlock
from service import app
from service.analysis_api import get_artifact_repository


def _auth_headers(user_id: str | None = "user-1") -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.AUTH_SECRET:
        headers["Authorization"] = f"Bearer {settings.AUTH_SECRET.get_secret_value()}"
    if user_id is not None:
        headers["X-Authenticated-User"] = user_id
    return headers


def _record(*, expires_at: datetime | None = None) -> AnalysisArtifactRecord:
    return AnalysisArtifactRecord.build(
        artifact_id="a" * 32,
        run_id="b" * 32,
        owner_user_id="user-1",
        thread_id="thread-1",
        model_key="scat-gbar-v23",
        model_version="23",
        analysis_result=AnalysisResult(
            dataset="scat",
            operation="run_regression_model",
            title="GBAR prediction",
            blocks=[
                MetricsBlock(
                    title="Summary",
                    items=[MetricItem(key="point_count", label="Points", value=36)],
                )
            ],
        ),
        created_at=datetime(2026, 9, 16, tzinfo=UTC),
        expires_at=expires_at,
    )


def _comparison_record(*, expires_at: datetime | None = None) -> AnalysisArtifactRecord:
    return AnalysisArtifactRecord.build(
        artifact_id="model_comparison-scat-comparison-v1-scat-batch-104363-h360-n1-test",
        run_id="c" * 32,
        owner_user_id="user-1",
        thread_id="thread-1",
        model_key="scat-comparison-v1",
        model_version="1",
        analysis_result=AnalysisResult(
            dataset="scat",
            operation="run_model_comparison",
            title="四方法轨迹预测对比",
            context={
                "batch": "scat20161015_20161021",
                "external_id": "104363",
                "visualization_snapshot_count": 6,
            },
            blocks=[
                MetricsBlock(
                    title="Summary",
                    items=[MetricItem(key="point_count", label="Points", value=180)],
                )
            ],
        ),
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
        expires_at=expires_at,
    )


@pytest.fixture
def analysis_api(monkeypatch):
    repository = AsyncMock()
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.TRUSTED_PROXY)
    app.dependency_overrides[get_artifact_repository] = lambda: repository
    client = TestClient(app, raise_server_exceptions=False)
    yield client, repository
    client.close()
    app.dependency_overrides.pop(get_artifact_repository, None)


def test_get_artifact_returns_owner_safe_contract(analysis_api):
    client, repository = analysis_api
    repository.get_for_owner.return_value = _record()

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_id"] == "a" * 32
    assert payload["thread_id"] == "thread-1"
    assert payload["analysis_result"]["title"] == "GBAR prediction"
    assert "owner_user_id" not in payload
    assert "payload_sha256" not in payload
    repository.get_for_owner.assert_awaited_once_with("a" * 32, owner_user_id="user-1")


def test_list_artifacts_returns_bounded_comparison_metadata(analysis_api):
    client, repository = analysis_api
    repository.list_for_owner.return_value = [_comparison_record()]

    response = client.get(
        "/api/v1/analysis/artifacts",
        params={
            "operation": "run_model_comparison",
            "batch": "scat20161015_20161021",
            "limit": 5,
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["artifacts"][0] == {
        "artifact_id": ("model_comparison-scat-comparison-v1-scat-batch-104363-h360-n1-test"),
        "model_key": "scat-comparison-v1",
        "model_version": "1",
        "dataset": "scat",
        "operation": "run_model_comparison",
        "title": "四方法轨迹预测对比",
        "batch": "scat20161015_20161021",
        "external_id": "104363",
        "snapshot_count": 6,
        "created_at": "2026-09-22T00:00:00Z",
        "expires_at": None,
    }
    assert "analysis_result" not in payload["artifacts"][0]
    assert "owner_user_id" not in payload["artifacts"][0]
    repository.list_for_owner.assert_awaited_once_with(
        owner_user_id="user-1",
        limit=5,
        operation="run_model_comparison",
        batch="scat20161015_20161021",
    )


def test_list_artifacts_omits_expired_records(analysis_api):
    client, repository = analysis_api
    repository.list_for_owner.return_value = [
        _comparison_record(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    ]

    response = client.get(
        "/api/v1/analysis/artifacts",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"count": 0, "artifacts": []}


def test_get_artifact_requires_proxy_identity(analysis_api):
    client, repository = analysis_api

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers(None),
    )

    assert response.status_code == 401
    repository.get_for_owner.assert_not_awaited()


def test_get_artifact_hides_missing_and_foreign_owned_ids(analysis_api):
    client, repository = analysis_api
    repository.get_for_owner.return_value = None

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers("user-2"),
    )

    assert response.status_code == 404
    repository.get_for_owner.assert_awaited_once_with("a" * 32, owner_user_id="user-2")


def test_get_artifact_rejects_expired_record(analysis_api):
    client, repository = analysis_api
    repository.get_for_owner.return_value = _record(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers(),
    )

    assert response.status_code == 404


def test_get_artifact_uses_configured_single_user_and_ignores_client_header(
    analysis_api, monkeypatch
):
    client, repository = analysis_api
    repository.get_for_owner.return_value = _record()
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.SINGLE_USER)
    monkeypatch.setattr(settings, "ANALYSIS_SINGLE_USER_ID", "user-1")

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers("attacker-controlled-header"),
    )

    assert response.status_code == 200
    repository.get_for_owner.assert_awaited_once_with("a" * 32, owner_user_id="user-1")


def test_get_artifact_rejects_incomplete_single_user_configuration(analysis_api, monkeypatch):
    client, repository = analysis_api
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.SINGLE_USER)
    monkeypatch.setattr(settings, "ANALYSIS_SINGLE_USER_ID", None)

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers(),
    )

    assert response.status_code == 503
    repository.get_for_owner.assert_not_awaited()


def test_get_artifact_fails_closed_when_identity_is_disabled(analysis_api, monkeypatch):
    client, repository = analysis_api
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.DISABLED)

    response = client.get(
        f"/api/v1/analysis/artifacts/{'a' * 32}",
        headers=_auth_headers(),
    )

    assert response.status_code == 503
    repository.get_for_owner.assert_not_awaited()


def test_analysis_artifact_openapi_contract_excludes_owner_identity(analysis_api):
    client, _ = analysis_api

    schema = client.get("/openapi.json", headers=_auth_headers()).json()
    operation = schema["paths"]["/api/v1/analysis/artifacts/{artifact_id}"]["get"]
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    response_schema = schema["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]

    assert operation["operationId"] == "get_analysis_artifact"
    assert "owner_user_id" not in response_schema["properties"]
