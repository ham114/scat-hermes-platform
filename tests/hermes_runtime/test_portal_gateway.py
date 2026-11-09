"""Production portal keeps the upstream key private and blocks unrelated writes/traversal."""

import http.server
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import serve_portal

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("portal-index")
    (tmp_path / "private.txt").write_text("private-never-served")
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @contextmanager
        def stream(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            yield serve_portal.httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(serve_portal.httpx, "Client", Client)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        serve_portal.handler(dist, "server-private-key", "https://portal.example", "HYN"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, method="GET", headers=None, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}" + path,
            method=method,
            headers=headers or {},
            data=body,
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    yield request, calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_static_history_and_private_paths(portal):
    request, calls = portal
    assert request("/settings") == (200, b"portal-index")
    assert request("/index.html", "HEAD") == (200, b"")
    assert request("/%2e%2e/private.txt")[0] == 403
    assert request("/missing.js")[0] == 404
    assert request("/portal-health", headers={"X-Portal-Authenticated-User": "other"})[0] == 403
    assert request("/portal-health", headers={"X-Portal-Authenticated-User": "HYN"})[0] == 200
    assert calls == []


def test_upstream_identity_and_author_credentials_are_separate(portal):
    request, calls = portal
    status, body = request(
        "/api/v1/hermes/workflow-catalog",
        headers={"Authorization": "client-basic-credential", "X-Authenticated-User": "other"},
    )
    assert status == 200 and json.loads(body) == {"status": "ok"}
    headers = calls[0][2]["headers"]
    assert headers["Authorization"] == "Bearer server-private-key"
    assert not any(k.lower() == "x-authenticated-user" for k in headers)
    assert request("/author-api/v1/settings", headers={"X-SCAT-Author-Key": "author-key"})[0] == 200
    assert calls[1][1] == "http://127.0.0.1:18124/v1/settings"
    assert not any(k.lower() == "authorization" for k in calls[1][2]["headers"])


def test_effect_routes_require_exact_origin_and_bounded_body(portal):
    request, calls = portal
    assert (
        request(
            "/api/v1/hermes/%2e%2e/scat/flights",
            "POST",
            {"Origin": "https://portal.example"},
            b"{}",
        )[0]
        == 403
    )
    assert (
        request("/api/v1/hermes/threads", "POST", {"Origin": "https://other.example"}, b"{}")[0]
        == 403
    )
    assert (
        request("/api/v1/scat/flights", "POST", {"Origin": "https://portal.example"}, b"{}")[0]
        == 404
    )
    assert (
        request(
            "/api/v1/hermes/threads",
            "POST",
            {"Origin": "https://portal.example", "Content-Length": "1000001"},
        )[0]
        == 413
    )
    assert calls == []
