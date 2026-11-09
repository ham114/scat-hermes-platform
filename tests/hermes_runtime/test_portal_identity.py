import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from application.context import ExecutionContext
from core import settings
from core.settings import AnalysisIdentityMode
from hermes_runtime import portal_identity
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.portal_identity import PortalDirectory
from hermes_runtime.registry import RunRegistry
from hermes_runtime.results import ToolResultStore
from service.hermes_api import router
from tests.hermes_runtime.test_platform_api import Native


@pytest.fixture
def directory(tmp_path, monkeypatch):
    body = {
        "schema_version": 1,
        "principals": {
            "HYN": {"owner_id": "scat-researcher", "role": "user"},
            "ZYJ": {"owner_id": "scat-zyj", "role": "user"},
            "admin": {"owner_id": "scat-admin", "role": "admin"},
        },
    }
    (tmp_path / "identities.json").write_text(json.dumps(body))
    key = tmp_path / "signing.key"
    key.write_text("test-key-" * 8)
    key.chmod(0o600)
    monkeypatch.setattr(portal_identity, "ROOT", tmp_path)
    monkeypatch.setattr(settings, "PORTAL_IDENTITIES_FILE", "identities.json")
    monkeypatch.setattr(settings, "PORTAL_SIGNING_KEY_FILE", "signing.key")
    return PortalDirectory(tmp_path, "identities.json", "signing.key")


def lower(headers):
    return {k.lower(): v for k, v in headers.items()}


def test_assertion_binds_subject_owner_method_target_and_expiry(directory):
    headers = lower(directory.sign("HYN", "GET", "/api/v1/hermes/threads?limit=20"))
    assert (
        directory.verify(headers, "GET", "/api/v1/hermes/threads?limit=20")["owner_id"]
        == "scat-researcher"
    )
    for method, target in [
        ("POST", "/api/v1/hermes/threads?limit=20"),
        ("GET", "/api/v1/hermes/threads?limit=21"),
    ]:
        with pytest.raises(ValueError):
            directory.verify(headers, method, target)
    for field, value in [
        ("x-portal-subject", "admin"),
        ("x-authenticated-user", "scat-zyj"),
        ("x-portal-issued-at", "1"),
    ]:
        with pytest.raises(ValueError):
            directory.verify(headers | {field: value}, "GET", "/api/v1/hermes/threads?limit=20")


@pytest.mark.asyncio
async def test_three_verified_users_cannot_read_or_control_each_others_threads_or_receipts(
    directory, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", AnalysisIdentityMode.TRUSTED_PROXY)
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("legacy-service-key"))
    registry = RunRegistry(tmp_path / "bindings.sqlite", workspace_root=tmp_path)
    results = ToolResultStore(tmp_path / "receipts.sqlite", workspace_root=tmp_path)
    native = Native()
    runtime = HermesHTTPRuntime(
        "http://127.0.0.1:18081",
        SecretStr("native-test-key-1234567890"),
        registry,
        transport=httpx.MockTransport(native),
        results=results,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.hermes_runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform"
    ) as client:
        threads = {}
        for username in ["HYN", "ZYJ", "admin"]:
            target = "/api/v1/hermes/threads"
            response = await client.post(
                target, json={}, headers=directory.sign(username, "POST", target)
            )
            assert response.status_code == 201
            threads[username] = response.json()["thread_id"]
        for username, own_thread in threads.items():
            target = "/api/v1/hermes/threads"
            response = await client.get(target, headers=directory.sign(username, "GET", target))
            assert [r["thread_id"] for r in response.json()["items"]] == [own_thread]
            for other, thread in threads.items():
                if other == username:
                    continue
                target = f"/api/v1/hermes/threads/{thread}/messages"
                assert (
                    await client.get(target, headers=directory.sign(username, "GET", target))
                ).status_code == 404
        target = "/api/v1/hermes/threads"
        assert (
            await client.get(
                target,
                headers={
                    "Authorization": "Bearer legacy-service-key",
                    "X-Authenticated-User": "scat-admin",
                },
            )
        ).status_code == 401
        own = threads["HYN"]
        context = ExecutionContext("scat-researcher", own)
        registry.bind(context, str(uuid4()), "run_private")
        reference = results.put(
            context.scope_key,
            "run_private",
            "call-private",
            "get_scat_track_points",
            {"status": "ok", "data": {}},
        )
        target = f"/api/v1/hermes/threads/{own}/runs/run_private/results/{reference['result_id']}"
        assert (
            await client.get(target, headers=directory.sign("ZYJ", "GET", target))
        ).status_code == 404
        assert (
            await client.get(target, headers=directory.sign("HYN", "GET", target))
        ).status_code == 200
    await runtime.aclose()


def test_portal_requires_private_front_token_and_rejects_non_admin_author_requests(
    directory, tmp_path, monkeypatch
):
    import http.server
    import threading
    import urllib.error
    import urllib.request

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from serve_portal import handler

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("index")
    portal = SimpleNamespace(directory=directory, front_key="private-front-test-key")
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), handler(dist, None, "https://portal.example", portal=portal)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path, headers):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}" + path, headers=headers
                )
            ) as response:
                assert response.headers.get("Cache-Control") == "no-store"
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    try:
        assert get("/portal-session", {"X-Portal-Authenticated-User": "HYN"})[0] == 403
        headers = {"X-Portal-Authenticated-User": "HYN", "X-Portal-Gateway-Token": portal.front_key}
        assert get("/portal-session", headers)[1]["owner_id"] == "scat-researcher"
        assert get("/author-api/v1/settings", headers)[0] == 403
        assert (
            get("/portal-session", headers | {"X-Portal-Authenticated-User": "admin"})[1]["role"]
            == "admin"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
