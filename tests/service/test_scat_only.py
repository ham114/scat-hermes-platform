import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr


@pytest.mark.asyncio
async def test_scat_only_lifespan_connects_without_loading_agents(monkeypatch):
    from service import scat_only

    connected = False
    closed = False

    async def connect() -> None:
        nonlocal connected
        connected = True

    async def close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(scat_only.flight_database, "connect", connect)
    monkeypatch.setattr(scat_only.flight_database, "close", close)

    application = FastAPI()
    async with scat_only.lifespan(application):
        assert application.state.configuration_status == "valid"

    assert connected
    assert closed


def test_importing_scat_only_does_not_import_agent_registry(tmp_path):
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    code = (
        "import sys; import service.scat_only; "
        "assert 'agents' not in sys.modules; "
        "assert 'service.service' not in sys.modules"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scat_only_exposes_versioned_routes_without_agent_endpoints():
    from service.scat_only import app

    paths = set(app.openapi()["paths"])

    assert "/api/v1/scat/batches" in paths
    assert "/api/v1/scat/flights" in paths
    assert "/api/v1/analysis/artifacts/{artifact_id}" in paths
    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert "/invoke" not in paths


def test_scat_only_routes_use_shared_auth(monkeypatch):
    from core import settings
    from service.scat_api import get_scat_service
    from service.scat_only import app

    scat_service = Mock()
    scat_service.list_batches = AsyncMock(
        return_value={
            "status": "ok",
            "data": {"count": 0, "total": 0, "has_more": False, "items": []},
        }
    )
    app.dependency_overrides[get_scat_service] = lambda: scat_service
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("scat-only-secret"))
    client = TestClient(app, raise_server_exceptions=False)

    denied = client.get("/api/v1/scat/batches")
    allowed = client.get(
        "/api/v1/scat/batches",
        headers={"Authorization": "Bearer scat-only-secret"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200

    client.close()
    app.dependency_overrides.pop(get_scat_service, None)
