"""Loopback, authenticated, independently budgeted DeepSeek relay for Honcho tests."""

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/honcho"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    import httpx
    from dotenv import dotenv_values

    os.umask(0o077)
    key = dotenv_values(DEPLOY / "secrets/honcho.env")["LLM_OPENAI_API_KEY"]
    access = (DEPLOY / "secrets/proxy.key").read_text().strip()
    state = DEPLOY / "testing/proxy-budget.sqlite"
    state.parent.mkdir(exist_ok=True)
    with sqlite3.connect(state) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, started REAL, reserved INTEGER, request_sha TEXT, status TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS campaign(id INTEGER PRIMARY KEY CHECK(id=1), started REAL)"
        )
        db.execute("INSERT OR IGNORE INTO campaign VALUES(1,?)", (time.time(),))
    state.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(100)

        def send(self, status, value):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.send(
                200 if self.path == "/health" else 404,
                {
                    "status": "ready",
                    "campaign_limit_requests": 60,
                    "campaign_limit_conservative": 500000,
                },
            )

        def do_POST(self):
            if not secrets.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + access
            ):
                self.send(401, {"error": {"message": "unauthorized"}})
                return
            if self.path not in {"/v1/chat/completions", "/chat/completions"}:
                self.send(404, {"error": {"message": "route_not_allowed"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 150000:
                    raise ValueError
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict) or value.get("stream"):
                    raise ValueError
                value["model"] = "deepseek-flash"
                value["thinking"] = {"type": "disabled"}
                maximum = value.get("max_tokens", value.get("max_completion_tokens", 2048))
                if type(maximum) is not int or not 1 <= maximum <= 4096:
                    raise ValueError
                if "max_tokens" not in value and "max_completion_tokens" not in value:
                    value["max_tokens"] = maximum
                raw = json.dumps(value, ensure_ascii=False).encode()
                reserved = len(raw) + maximum + 32
            except (ValueError, TypeError):
                self.send(400, {"error": {"message": "invalid_bounded_request"}})
                return
            with sqlite3.connect(state, timeout=10) as db:
                db.execute("BEGIN IMMEDIATE")
                count, used = db.execute(
                    "SELECT count(*),COALESCE(sum(reserved),0) FROM requests"
                ).fetchone()
                started = db.execute("SELECT started FROM campaign WHERE id=1").fetchone()[0]
                if (
                    count + 2 >= 60
                    or used + 941 + reserved > 500000
                    or time.time() - started > 3600
                ):
                    self.send(429, {"error": {"message": "honcho_test_budget_exhausted"}})
                    return
                identity = db.execute(
                    "INSERT INTO requests(started,reserved,request_sha,status) VALUES(?,?,?,'reserved')",
                    (time.time(), reserved, hashlib.sha256(raw).hexdigest()),
                ).lastrowid
            status = "unknown"
            inp = out = 0
            try:
                with httpx.Client(trust_env=False, timeout=90) as client:
                    response = client.post(
                        "https://api.deepseek.com/chat/completions",
                        headers={"Authorization": "Bearer " + key},
                        json=value,
                    )
                data = response.json()
                if response.status_code == 200:
                    usage = data.get("usage") or {}
                    inp, out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                    status = "ok"
                    self.send(200, data)
                else:
                    status = "http_" + str(response.status_code)
                    message = str(
                        (data.get("error") or {}).get("message", "upstream_error")
                    ).replace(key, "[REDACTED]")[:300]
                    self.send(
                        response.status_code,
                        {"error": {"message": message, "type": "upstream_error"}},
                    )
            except Exception as exc:  # noqa: BLE001 -- sanitize failures at the credential boundary
                status = type(exc).__name__
                self.send(503, {"error": {"message": "upstream_outcome_unknown"}})
            finally:
                with sqlite3.connect(state) as db:
                    db.execute(
                        "UPDATE requests SET status=?,input_tokens=?,output_tokens=? WHERE id=?",
                        (status, inp, out, identity),
                    )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 18142), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
