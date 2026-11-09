"""Writable loopback workspace; SSH and backend credentials never enter browser or Vite."""

import argparse
import getpass
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import paramiko
from preview_remote_frontend import ROOT, SSHConnection


def allowed_request(method, path, author=False):
    if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n\\"):
        return False
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        return False
    if author:
        return (
            path.startswith(("/v1/skills/", "/v1/settings")) or path.split("?")[0] == "/v1/skills"
        )
    return path.startswith("/api/v1/") and (
        method in {"GET", "HEAD"} or path.startswith("/api/v1/hermes/")
    )


def handler(connection, token, frontend_port, remote_port, *, author=False):
    class Forward(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # No URLs, headers, conversations or credentials in access logs.

        def relay(self):
            self.close_connection = True
            channel = None
            try:
                self.connection.settimeout(30)
                origins = {f"http://127.0.0.1:{frontend_port}", f"http://localhost:{frontend_port}"}
                if self.headers.get("Origin") and self.headers["Origin"] not in origins:
                    self.send_error(403, "Local workspace origin required")
                    return
                if not allowed_request(self.command, self.path, author):
                    self.send_error(405, "Route or method unavailable")
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or self.headers.get("Transfer-Encoding"):
                    self.send_error(400, "Unambiguous content length required")
                    return
                length = int(lengths[0]) if lengths else 0
                if not 0 <= length <= 1_000_000:
                    self.send_error(413, "Request too large")
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    return
                headers = [
                    f"{self.command} {self.path} HTTP/1.1",
                    f"Host: 127.0.0.1:{remote_port}",
                    "Connection: close",
                ]
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
                }
                for key, value in self.headers.items():
                    if key.lower() not in dropped:
                        headers.append(key + ": " + value)
                if not author:
                    headers.append("Authorization: Bearer " + token)
                headers.append("Content-Length: " + str(length))
                raw = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + body
                # Reconnect only before dispatch; never resend a partially dispatched request.
                channel = connection.get().open_channel(
                    "direct-tcpip", ("127.0.0.1", remote_port), self.client_address, timeout=15
                )
                channel.sendall(raw)
                while True:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.connection.sendall(data)
            except (OSError, ValueError, EOFError, paramiko.SSHException):
                # A write may already have reached the backend. Browser recovery uses original IDs.
                return
            finally:
                if channel:
                    channel.close()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = relay

    return Forward


def main(args):
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    node = shutil.which("node")
    if not node or not subprocess.check_output([node, "--version"], text=True).startswith("v24."):
        raise RuntimeError("Existing Node 24 required; no dependencies will be installed")
    if not (ROOT / "frontend/node_modules/vite/bin/vite.js").is_file():
        raise RuntimeError("Existing frontend dependencies are missing")
    for port in [args.port, args.proxy_port, args.author_proxy_port]:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    if args.credentials_stdin:
        values = json.loads(sys.stdin.read(4097))
        if set(values) != {"password"}:
            raise ValueError("Only SSH password is accepted")
        password = values.pop("password")
    else:
        password = getpass.getpass("SSH password for h3c@172.26.67.148: ")
    connection = SSHConnection(password)
    password = None
    child = None
    temporary = None
    servers = []
    logs = ROOT / "var/preview/writable"
    logs.mkdir(parents=True, exist_ok=True)
    stop_file = logs / ("stop-" + str(os.getpid()) + ".request")
    record_path = logs / ("services-" + str(args.port) + ".json")
    try:
        connection.get()
        remote = """import json,subprocess
row=json.loads(subprocess.check_output(['docker','inspect','scat-hermes-h7b-platform-1']))[0]
env=dict(x.split('=',1) for x in row['Config']['Env'] if '=' in x)
print(json.dumps({'token':env.get('AUTH_SECRET'),'running':row['State']['Running']}))
"""
        import shlex

        command = (
            "cd /home/h3c/project/python/scat-hermes-platform && /home/h3c/anaconda3/envs/scat-agent/bin/python -B -c "
            + shlex.quote(remote)
        )
        _, out, _ = connection.client.exec_command(command, timeout=20)
        value = json.loads(out.read().decode())
        if out.channel.recv_exit_status() or not value.get("running") or not value.get("token"):
            raise RuntimeError("Backend authentication or running service unavailable")
        token = value.pop("token")
        temporary = ROOT / ".tmp/writable-live" / ("preview-" + uuid4().hex)
        temporary.mkdir(parents=True)
        for port, remote_port, author in [
            (args.proxy_port, 18120, False),
            (args.author_proxy_port, 18124, True),
        ]:
            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", port),
                handler(
                    connection, token if not author else "", args.port, remote_port, author=author
                ),
            )
            server.daemon_threads = True
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        env = os.environ | {
            "AUTH_SECRET": "",
            "VITE_WORKSPACE_READONLY": "0",
            "VITE_SCAT_READONLY_PREVIEW": "0",
            "SCAT_DEV_PROXY_TARGET": f"http://127.0.0.1:{args.proxy_port}",
            "SCAT_AUTHOR_PROXY_TARGET": f"http://127.0.0.1:{args.author_proxy_port}",
            "SCAT_FRONTEND_CACHE_DIR": str(temporary / "vite-cache"),
            "NODE_COMPILE_CACHE": str(temporary / "node-cache"),
            **{key: str(temporary) for key in ["TEMP", "TMP", "TMPDIR"]},
        }
        with (logs / "vite.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(
                [
                    node,
                    str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.port),
                    "--strictPort",
                ],
                cwd=ROOT / "frontend",
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        record = {
            "controller_pid": os.getpid(),
            "vite_pid": child.pid,
            "url": f"http://127.0.0.1:{args.port}/",
            "writable": True,
            "started_at": time.time(),
            "expires_at": time.time() + args.hours * 3600,
            "temporary": str(temporary),
            "proxy_ports": [args.proxy_port, args.author_proxy_port],
        }
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"url": record["url"], "writable": True, "stop": "Ctrl+C", "hours": args.hours}
            ),
            flush=True,
        )
        if args.open_browser:
            import urllib.request
            import webbrowser

            ready_until = time.monotonic() + 10
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < ready_until:
                try:
                    with opener.open(record["url"], timeout=1):
                        webbrowser.open(record["url"])
                        break
                except OSError:
                    time.sleep(0.5)
        deadline = time.monotonic() + args.hours * 3600
        while child.poll() is None and time.monotonic() < deadline and not stop_file.exists():
            time.sleep(1)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for server in servers:
            server.shutdown()
            server.server_close()
        connection.close()
        if (
            temporary
            and temporary.parent.resolve() == (ROOT / ".tmp/writable-live").resolve()
            and not temporary.is_symlink()
        ):
            shutil.rmtree(temporary)
        stop_file.unlink(missing_ok=True)
        path = record_path
        if path.exists():
            record = json.loads(path.read_text())
            record["stopped"] = True
            path.write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=4)
    parser.add_argument("--port", type=int, default=5175)
    parser.add_argument("--proxy-port", type=int, default=18152)
    parser.add_argument("--author-proxy-port", type=int, default=18153)
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop this workspace port and clean its temporary directory",
    )
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (
        not 0 < args.hours <= 8
        or len({args.port, args.proxy_port, args.author_proxy_port}) != 3
        or any(not 1024 <= p <= 65535 for p in [args.port, args.proxy_port, args.author_proxy_port])
    ):
        parser.error("Distinct unprivileged ports and duration <=8 hours required")
    if args.stop:
        path = ROOT / "var/preview/writable" / ("services-" + str(args.port) + ".json")
        if path.exists():
            record = json.loads(path.read_text())
            if not record.get("stopped"):
                (path.parent / ("stop-" + str(int(record["controller_pid"])) + ".request")).touch()
                print("Stop requested; the controller will close ports and clean temporary files.")
        raise SystemExit(0)
    try:
        main(args)
    except KeyboardInterrupt:
        print("Workspace stopped; temporary files removed.", flush=True)
    except Exception as exc:  # noqa: BLE001 -- never print credentials or transport payloads
        print("Workspace could not start or disconnected: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
