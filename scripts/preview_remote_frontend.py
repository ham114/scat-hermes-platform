"""Local existing-Conda SSH forward plus Node 24 frontend. Credentials arrive on stdin."""

import argparse
import json
import os
import select
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/home/h3c/project/python/scat-hermes-platform"


class SSHConnection:
    """Keep credentials only in memory and serialize bounded reconnect attempts."""

    def __init__(self, password):
        self.password = password
        self.client = None
        self.lock = threading.RLock()
        self.reconnects = 0

    def get(self):
        with self.lock:
            if self.client is not None:
                transport = self.client.get_transport()
                if transport is not None and transport.is_active():
                    return transport
                self.client.close()
                self.reconnects += 1
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            for attempt in range(4):
                try:
                    client.connect(
                        "172.26.67.148",
                        username="h3c",
                        password=self.password,
                        timeout=6,
                        auth_timeout=8,
                        banner_timeout=8,
                        look_for_keys=False,
                        allow_agent=False,
                    )
                    break
                except (OSError, paramiko.SSHException, EOFError):
                    if attempt == 3:
                        client.close()
                        raise
                    time.sleep(1)
            self.client = client
            transport = client.get_transport()
            transport.set_keepalive(10)
            return transport

    def close(self):
        with self.lock:
            if self.client is not None:
                self.client.close()
            self.password = None


def main(hours, *, test_reconnect=False):
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    credentials = json.loads(sys.stdin.read(4097))
    password = credentials.pop("password")
    if credentials:
        raise ValueError("Only the SSH password is accepted on stdin")
    node = shutil.which("node")
    if not node or not subprocess.check_output([node, "--version"], text=True).startswith("v24."):
        raise RuntimeError("Existing Node 24 is required")
    connection = SSHConnection(password)
    password = None
    connection.get()
    # The remote service token stays only in this process and the Vite server environment.
    # It is never put into a browser variable, URL, file or stdout.
    command = (
        "cd "
        + REMOTE
        + " && /home/h3c/anaconda3/envs/scat-agent/bin/python -B -c \"import json;from dotenv import dotenv_values;print(json.dumps(dotenv_values('.env').get('AUTH_SECRET')))\""
    )
    _, output, _error = connection.client.exec_command(command, timeout=15)
    secret = json.loads(output.read().decode())
    if output.channel.recv_exit_status() != 0:
        raise RuntimeError("Remote proxy configuration unavailable")
    home = ROOT / "var/demo/local"
    home.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / ".tmp/s5-demo"
    temporary.mkdir(parents=True, exist_ok=True)

    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                channel = connection.get().open_channel(
                    "direct-tcpip", ("127.0.0.1", 18080), self.client_address, timeout=10
                )
            except (OSError, paramiko.SSHException, EOFError):
                self.request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            try:
                while True:
                    ready, _, _ = select.select([self.request, channel], [], [], 1)
                    if self.request in ready:
                        data = self.request.recv(65536)
                        if not data:
                            break
                        channel.sendall(data)
                    if channel in ready:
                        data = channel.recv(65536)
                        if not data:
                            break
                        self.request.sendall(data)
            except (OSError, EOFError, paramiko.SSHException):
                pass
            finally:
                channel.close()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 18080), Forward)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ | {
        "SCAT_DEV_PROXY_TARGET": "http://127.0.0.1:18080",
        "SCAT_FRONTEND_CACHE_DIR": str(home / "vite-cache"),
        "AUTH_SECRET": secret or "",
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "TMPDIR": str(temporary),
    }
    child = None
    try:
        with (home / "vite.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(
                [
                    node,
                    str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "5173",
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
            "started_at": time.time(),
            "expires_at": time.time() + hours * 3600,
            "url": "http://127.0.0.1:5173/assistant",
            "remote": "h3c@172.26.67.148:18080",
            "ssh_reconnects": 0,
            "reconnect_test_requested": test_reconnect,
        }
        (home / "services.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(record["url"], flush=True)
        if test_reconnect:
            connection.get().close()
        until = time.monotonic() + hours * 3600
        while child.poll() is None and time.monotonic() < until:
            try:
                connection.get()
            except (OSError, paramiko.SSHException, EOFError):
                time.sleep(2)
            if record["ssh_reconnects"] != connection.reconnects:
                record["ssh_reconnects"] = connection.reconnects
                (home / "services.json").write_text(
                    json.dumps(record, indent=2) + "\n", encoding="utf-8"
                )
            time.sleep(1)
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        server.shutdown()
        server.server_close()
        connection.close()
        assert (
            temporary.resolve() == (ROOT / ".tmp/s5-demo").resolve() and not temporary.is_symlink()
        )
        shutil.rmtree(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=4)
    parser.add_argument(
        "--test-reconnect-once",
        action="store_true",
        help="Maintainer check: close one SSH transport, keep Vite alive and reconnect",
    )
    args = parser.parse_args()
    if not 0 < args.hours <= 8:
        parser.error("Preview duration must be at most 8 hours")
    main(args.hours, test_reconnect=args.test_reconnect_once)
