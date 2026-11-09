"""Finite live gateway/owner checks without paid model calls or inspecting user messages."""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.portal_identity import private_key


def main():
    out = ROOT / "var/validation/20261108-release-readiness"
    assert not (out / "identity-live-private.json").exists(), "already_completed"
    key = private_key(ROOT, "var/deploy/portal/keys/front.key")
    identities = json.loads((ROOT / "var/deploy/portal/identities.json").read_text())["principals"]
    threads = {}
    cases = []
    with httpx.Client(base_url="http://127.0.0.1:18159", trust_env=False, timeout=15) as client:

        def request(user, method, path, **kwargs):
            headers = {
                "X-Portal-Gateway-Token": key,
                "X-Portal-Authenticated-User": user,
                "Origin": "https://172.26.67.148:8444",
            }
            headers.update(kwargs.pop("headers", {}))
            return client.request(method, path, headers=headers, **kwargs)

        assert (
            client.get(
                "/portal-session", headers={"X-Portal-Authenticated-User": "admin"}
            ).status_code
            == 403
        )
        cases.append("unsigned_loopback_rejected")
        for user, principal in identities.items():
            response = request(user, "GET", "/portal-session")
            assert response.status_code == 200
            assert response.json() == {"username": user, **principal}
            response = request(user, "POST", "/api/v1/hermes/threads", json={})
            assert response.status_code == 201, response.text[:200]
            threads[user] = response.json()["thread_id"]
            response = request(user, "GET", "/api/v1/hermes/threads?limit=100")
            assert response.status_code == 200
            assert threads[user] in [r["thread_id"] for r in response.json()["items"]]
            response = request(user, "GET", "/author-api/v1/settings")
            assert response.status_code == (401 if user == "admin" else 403), response.status_code
            cases.append(user + ":identity_thread_role")
        for user in identities:
            for other, thread in threads.items():
                if user == other:
                    continue
                response = request(user, "GET", f"/api/v1/hermes/threads/{thread}/messages")
                assert response.status_code == 404
                response = request(user, "GET", f"/api/v1/hermes/threads/{thread}/model-tasks")
                assert response.status_code == 404
                cases.append(user + ":cannot_read_" + other)
        path = "/api/v1/hermes/threads/42297b89-54c3-469d-94c0-13bccae5b9e2/model-tasks/7b0dbcfd9cf846898ed3183b44a13e18/saved-model-output"
        response = request("HYN", "GET", path)
        assert response.status_code == 200, response.text[:200]
        expected = json.loads((out / "saved-gbar-output.json").read_text())
        assert response.json()["payload_sha256"] == expected["payload_sha256"]
        assert request("ZYJ", "GET", path).status_code == 404
        assert request("admin", "GET", path).status_code == 404
        cases.append("real_saved_gbar_output_owner_only")
        response = request(
            "ZYJ",
            "GET",
            "/portal-session",
            headers={"X-Authenticated-User": "scat-researcher", "X-Portal-Subject": "HYN"},
        )
        assert response.json()["owner_id"] == "scat-zyj"
        cases.append("browser_owner_header_ignored")
    private = out / "private"
    private.mkdir(exist_ok=True)
    (private / "identity-threads.json").write_text(json.dumps(threads))
    report = {
        "status": "passed",
        "kind": "live_new_gateway_assertions_not_three_browser_password_logins",
        "cases": cases,
        "new_model_calls": 0,
        "private_messages_read": 0,
        "users": list(identities),
    }
    (out / "identity-live-private.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
