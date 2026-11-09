"""Loopback production assets and scoped API relay; credentials remain in server memory."""

import argparse
import http.server
import json
import mimetypes
import secrets
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]


def backend_key():
    command = "/usr/bin/docker inspect scat-hermes-h7b-platform-1"
    result = subprocess.run(
        ["/usr/bin/sg", "docker", "-c", command], capture_output=True, check=False, timeout=15
    )
    if result.returncode:
        raise RuntimeError("portal_backend_configuration_unavailable")
    row = json.loads(result.stdout)[0]
    value = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x).get("AUTH_SECRET")
    if not row["State"]["Running"] or not value:
        raise RuntimeError("portal_backend_unavailable")
    return value


def handler(dist, secret, origin, allowed_user=None, portal=None):
    dist = dist.resolve()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, body, kind="application/json"):
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            if portal is not None and (
                kind == "application/json" or self.path.startswith(("/api/", "/author-api/"))
            ):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def handle_request(self):
            self.close_connection = True
            gateway_user = self.headers.get("X-Portal-Authenticated-User")
            path = urlsplit(self.path).path
            principal = None
            if portal is not None:
                if (
                    path == "/portal-health"
                    and gateway_user is None
                    and self.command in {"GET", "HEAD"}
                ):
                    return self.reply(200, b'{"status":"ready"}')
                if not secrets.compare_digest(
                    self.headers.get("X-Portal-Gateway-Token", ""), portal.front_key
                ):
                    return self.reply(403, b'{"error":"trusted_gateway_required"}')
                try:
                    principal = portal.directory.principal(gateway_user)
                except ValueError:
                    return self.reply(403, b'{"error":"portal_user_not_enabled"}')
            if (
                allowed_user is not None
                and gateway_user is not None
                and gateway_user != allowed_user
            ):
                return self.reply(403, b'{"error":"portal_user_not_enabled"}')
            decoded_path = unquote(path)
            if (
                any(part in {".", ".."} for part in decoded_path.split("/"))
                or "\\" in decoded_path
                or decoded_path.startswith("//")
            ):
                return self.reply(403, b"{}")
            if (
                not path.startswith("/")
                or path.startswith("//")
                or any(x in self.path for x in ["\r", "\n", "\\"])
            ):
                return self.reply(400, b"{}")
            if self.command not in {"GET", "HEAD"} and self.headers.get("Origin") != origin:
                return self.reply(403, b'{"error":"same_origin_required"}')
            if path == "/portal-health" and self.command in {"GET", "HEAD"}:
                return self.reply(200, b'{"status":"ready"}')
            if (
                path == "/portal-session"
                and principal is not None
                and self.command in {"GET", "HEAD"}
            ):
                return self.reply(200, json.dumps(principal).encode())
            author = (
                path.startswith(("/author-api/v1/skills/", "/author-api/v1/settings"))
                or path == "/author-api/v1/skills"
            )
            business = path.startswith("/api/v1/") and (
                self.command in {"GET", "HEAD"} or path.startswith("/api/v1/hermes/")
            )
            if author or business:
                if author and principal is not None and principal["role"] != "admin":
                    return self.reply(403, b'{"error":"administrator_required"}')
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or self.headers.get("Transfer-Encoding"):
                    return self.reply(400, b"{}")
                try:
                    length = int(lengths[0]) if lengths else 0
                except ValueError:
                    return self.reply(400, b"{}")
                if not 0 <= length <= 1000000:
                    return self.reply(413, b"{}")
                self.connection.settimeout(30)
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.reply(400, b"{}")
                dropped = {
                    "host",
                    "connection",
                    "proxy-connection",
                    "keep-alive",
                    "transfer-encoding",
                    "content-length",
                    "authorization",
                    "x-authenticated-user",
                    "upgrade",
                    "expect",
                    "cookie",
                    "x-portal-authenticated-user",
                }
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in dropped and not k.lower().startswith("x-portal-")
                }
                if portal is not None:
                    signed_target = self.path.removeprefix("/author-api") if author else self.path
                    headers.update(portal.directory.sign(gateway_user, self.command, signed_target))
                elif not author:
                    headers["Authorization"] = "Bearer " + secret
                target = (
                    ("http://127.0.0.1:18124" + self.path.removeprefix("/author-api"))
                    if author
                    else ("http://127.0.0.1:18120" + self.path)
                )
                started = False
                try:
                    with (
                        httpx.Client(
                            trust_env=False, timeout=httpx.Timeout(650, connect=3)
                        ) as client,
                        client.stream(
                            self.command, target, content=body, headers=headers
                        ) as response,
                    ):
                        self.send_response(response.status_code)
                        for key, value in response.headers.items():
                            if key.lower() not in {
                                "connection",
                                "transfer-encoding",
                                "content-length",
                                "content-encoding",
                                *({"cache-control"} if portal is not None else set()),
                            }:
                                self.send_header(key, value)
                        if portal is not None:
                            self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.send_header("X-Accel-Buffering", "no")
                        self.end_headers()
                        started = True
                        if self.command != "HEAD":
                            for chunk in response.iter_bytes():
                                self.wfile.write(chunk)
                                self.wfile.flush()
                except (httpx.HTTPError, OSError):
                    if not started:
                        self.reply(
                            503,
                            b'{"error":{"code":"portal_upstream_unavailable","outcome_unknown":true}}',
                        )
                return
            if path.startswith(("/api/", "/author-api/")):
                return self.reply(404, b"{}")
            if self.command not in {"GET", "HEAD"}:
                return self.reply(405, b"{}")
            relative = unquote(path).lstrip("/")
            candidate = dist / relative
            resolved = candidate.resolve()
            if (
                not resolved.is_relative_to(dist)
                or ".." in Path(relative).parts
                or candidate.is_symlink()
            ):
                return self.reply(403, b"{}")
            if not resolved.is_file():
                if Path(relative).suffix:
                    return self.reply(404, b"{}")
                resolved = dist / "index.html"
            mime = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".wasm": "application/wasm",
                ".json": "application/json",
            }.get(
                resolved.suffix,
                mimetypes.guess_type(str(resolved))[0] or "application/octet-stream",
            )
            self.reply(200, resolved.read_bytes(), mime)

        do_GET = handle_request
        do_HEAD = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_PATCH = handle_request
        do_DELETE = handle_request
        do_OPTIONS = handle_request

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--port", type=int, default=18158)
    parser.add_argument("--origin", default="https://172.26.67.148:8444")
    parser.add_argument("--user")
    args = parser.parse_args()
    release = (ROOT / args.release).resolve()
    assert release.is_relative_to(ROOT / "var/deploy/portal/releases")
    manifest = json.loads((release / "manifest.json").read_text())
    import hashlib

    assert all(
        hashlib.sha256((release / "dist" / p).read_bytes()).hexdigest() == sha
        for p, sha in manifest["files"].items()
    )
    http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler(release / "dist", backend_key(), args.origin, args.user)
    ).serve_forever()


if __name__ == "__main__":
    main()
