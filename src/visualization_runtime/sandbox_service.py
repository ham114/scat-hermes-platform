"""Loopback-only authenticated launcher API, with no source/path/image tool parameters."""

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from visualization_runtime.launcher import RenderLauncher
from visualization_runtime.sandbox import RenderError
from visualization_runtime.store import JobConflict, canonical


def server(launcher, key, *, port=18166, concurrency=2):
    if len(key) < 32:
        raise ValueError("sandbox_service_key_required")
    slots = threading.BoundedSemaphore(concurrency)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            raw = canonical(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != "/health":
                self.reply(404, {"error": "not_found"})
                return
            self.reply(200, {"status": "alive", "readiness_is_not_task_completion": True})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key):
                self.reply(403, {"error": "forbidden"})
                return
            acquired = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("invalid_request")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError("invalid_request")
                if self.path == "/stopped" and set(body) == {"resource_id"}:
                    self.reply(200, {"stopped": launcher.sandbox.stopped(body["resource_id"])})
                    return
                required = {"task_id", "generation"} | (
                    {"worker", "attempt_id"} if self.path == "/render" else set()
                )
                if (
                    set(body) != required
                    or not re.fullmatch(r"[0-9a-f]{32}", body["task_id"])
                    or type(body["generation"]) is not int
                    or not 1 <= body["generation"] <= 999999999
                ):
                    raise ValueError("invalid_request")
                if self.path == "/render":
                    if not re.fullmatch(r"[0-9a-f]{32}", body["attempt_id"]) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,128}", body["worker"]
                    ):
                        raise ValueError("invalid_request")
                    acquired = slots.acquire(blocking=False)
                    if not acquired:
                        self.reply(503, {"error": "sandbox_busy"})
                        return
                    result = launcher.render(
                        body["task_id"], body["worker"], body["generation"], body["attempt_id"]
                    )
                elif self.path == "/cleanup":
                    launcher.cleanup_task(body["task_id"], body["generation"])
                    result = {"state": "cleanup_checked"}
                else:
                    self.reply(404, {"error": "not_found"})
                    return
                self.reply(200, result)
            except (JobConflict, RenderError):
                self.reply(409, {"error": "sandbox_operation_rejected"})
            except (ValueError, TypeError, KeyError):
                self.reply(400, {"error": "invalid_request"})
            except Exception:  # noqa: BLE001 -- HTTP boundary must never disclose source or data
                self.reply(500, {"error": "sandbox_internal_error"})
            finally:
                if acquired:
                    slots.release()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


class SandboxClient(RenderLauncher):
    def __init__(self, jobs, snapshots, url, key, *, image, timeout=45):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("fixed_loopback_sandbox_origin_required")
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout
        super().__init__(jobs, snapshots, self)
        self.image = image

    def _request(self, path, body):
        try:
            with httpx.Client(
                trust_env=False, follow_redirects=False, timeout=self.timeout
            ) as client:
                response = client.post(
                    self.url + path, json=body, headers={"Authorization": "Bearer " + self.key}
                )
            if response.status_code != 200 or len(response.content) > 32768:
                raise RenderError("sandbox_service_unavailable")
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise RenderError("sandbox_service_unavailable") from None

    def render(self, task_id, worker, generation, attempt_id):
        return self._request(
            "/render",
            {
                "task_id": task_id,
                "worker": worker,
                "generation": generation,
                "attempt_id": attempt_id,
            },
        )

    def cleanup_task(self, task_id, generation):
        return self._request("/cleanup", {"task_id": task_id, "generation": generation})

    def stopped(self, name):
        try:
            return self._request("/stopped", {"resource_id": name}).get("stopped") is True
        except RenderError:
            return False
