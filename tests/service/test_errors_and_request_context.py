from unittest.mock import AsyncMock
from uuid import UUID

from pydantic import SecretStr


def test_server_generates_request_id(test_client) -> None:
    response = test_client.get("/health/live")

    assert response.status_code == 200
    UUID(response.headers["X-Request-ID"])


def test_safe_caller_request_id_is_preserved(test_client) -> None:
    response = test_client.get(
        "/health/live",
        headers={"X-Request-ID": "client-trace-123"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "client-trace-123"


def test_invalid_caller_request_id_is_replaced(test_client) -> None:
    response = test_client.get(
        "/health/live",
        headers={"X-Request-ID": "x" * 129},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "x" * 129
    UUID(response.headers["X-Request-ID"])


def test_http_exception_uses_safe_error_schema(mock_settings, test_client) -> None:
    mock_settings.AUTH_SECRET = SecretStr("test-secret")

    response = test_client.post(
        "/invoke",
        json={"message": "test"},
        headers={"X-Request-ID": "auth-failure-1"},
    )

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "auth-failure-1"
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "Missing or invalid bearer token",
            "request_id": "auth-failure-1",
        }
    }


def test_validation_error_does_not_echo_rejected_input(mock_settings, test_client) -> None:
    mock_settings.AUTH_SECRET = None
    secret_input = "should-not-be-returned"

    response = test_client.post(
        "/invoke",
        json={"unexpected": secret_input},
        headers={"X-Request-ID": "validation-failure-1"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"] == "validation-failure-1"
    assert secret_input not in response.text


def test_internal_error_is_safe_and_traceable(mock_settings, mock_agent, test_client) -> None:
    mock_settings.AUTH_SECRET = None
    mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("private failure detail"))

    response = test_client.post(
        "/invoke",
        json={"message": "test"},
        headers={"X-Request-ID": "internal-failure-1"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": "internal-failure-1",
        }
    }
    assert "private failure detail" not in response.text


def test_unknown_agent_is_a_traceable_404(
    mock_settings,
    test_client,
    caplog,
) -> None:
    mock_settings.AUTH_SECRET = None

    response = test_client.post(
        "/not-a-real-agent/invoke",
        json={"message": "test"},
        headers={"X-Request-ID": "missing-agent-1"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "Agent 'not-a-real-agent' was not found",
        "request_id": "missing-agent-1",
    }
    assert "request_id=missing-agent-1" in caplog.text
    assert "status_code=404" in caplog.text
