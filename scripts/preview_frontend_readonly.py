"""User-authorized localhost preview. GET/HEAD only; no backend writes or model admissions."""

import argparse
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from preview_remote_frontend import ROOT, SSHConnection


def main(hours):
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda required")
    node = shutil.which("node")
    if not node or not subprocess.check_output([node, "--version"], text=True).startswith("v24."):
        raise RuntimeError("Existing Node 24 required")
    for port in (5174, 18151):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    values = json.loads(sys.stdin.read(4097))
    connection = SSHConnection(values.pop("password"))
    if values:
        raise ValueError("Only SSH authentication is accepted")
    connection.get()
    # Explicitly authorized in this chat: backend token remains in process memory,
    # never a VITE variable, file, URL, browser value, or log.
    command = (
        "cd /home/h3c/project/python/scat-hermes-platform && "
        "/home/h3c/anaconda3/envs/scat-agent/bin/python -B -c "
        '"import json,subprocess;'
        "row=json.loads(subprocess.check_output(['docker','inspect','scat-hermes-h7b-platform-1']))[0];"
        "print(json.dumps(next((x.split('=',1)[1] for x in row['Config']['Env'] if x.startswith('AUTH_SECRET=')),None)))\""
    )
    _, stream, _ = connection.client.exec_command(command, timeout=20)
    token = json.loads(stream.read().decode())
    if stream.channel.recv_exit_status() or not isinstance(token, str) or not token:
        connection.close()
        raise RuntimeError("Backend authentication unavailable")
    temporary = ROOT / ".tmp/frontend-workspace" / ("readonly-preview-" + uuid4().hex)
    temporary.mkdir(parents=True)
    logs = ROOT / "var/validation/20261028-frontend-workspace"
    logs.mkdir(parents=True, exist_ok=True)

    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(15)
            raw = b""
            try:
                while b"\r\n\r\n" not in raw and len(raw) <= 32768:
                    data = self.request.recv(4096)
                    if not data:
                        return
                    raw += data
                header, boundary, body = raw.partition(b"\r\n\r\n")
                lines = header.split(b"\r\n")
                parts = lines[0].split()
                fields = [line.split(b":", 1) for line in lines[1:]]
                valid = (
                    boundary
                    and not body
                    and len(raw) <= 32768
                    and len(parts) == 3
                    and parts[0] in {b"GET", b"HEAD"}
                    and parts[1].startswith(b"/api/v1/")
                    and parts[2] == b"HTTP/1.1"
                    and all(len(field) == 2 for field in fields)
                    and not any(
                        k.lower() == b"transfer-encoding"
                        or k.lower() == b"content-length"
                        and v.strip() != b"0"
                        for k, v in fields
                    )
                )
                if not valid:
                    self.request.sendall(
                        b"HTTP/1.1 405 Method Not Allowed\r\nAllow: GET, HEAD\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
                # Exactly one request per channel. Never relay pipelined requests.
                kept = [
                    line
                    for line in lines[1:]
                    if line.split(b":", 1)[0].lower()
                    not in {b"connection", b"proxy-connection", b"keep-alive"}
                ]
                request = b"\r\n".join([lines[0], *kept, b"Connection: close", b"", b""])
                channel = connection.get().open_channel(
                    "direct-tcpip", ("127.0.0.1", 18120), self.client_address, timeout=15
                )
                try:
                    channel.sendall(request)
                    while True:
                        data = channel.recv(65536)
                        if not data:
                            break
                        self.request.sendall(data)
                finally:
                    channel.close()
            except Exception:  # noqa: BLE001 -- no transport or credential details in output
                return

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 18151), Forward)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    child = None
    try:
        env = os.environ | {
            "AUTH_SECRET": token,
            "VITE_WORKSPACE_READONLY": "1",
            "SCAT_DEV_PROXY_TARGET": "http://127.0.0.1:18151",
            "SCAT_FRONTEND_CACHE_DIR": str(temporary / "vite-cache"),
            **{key: str(temporary) for key in ("TMP", "TEMP", "TMPDIR")},
        }
        token = None
        with (logs / "readonly-preview.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(
                [
                    node,
                    str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "5174",
                    "--strictPort",
                ],
                cwd=ROOT / "frontend",
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        env.pop("AUTH_SECRET", None)
        record = {
            "controller_pid": os.getpid(),
            "vite_pid": child.pid,
            "url": "http://127.0.0.1:5174/",
            "readonly": True,
            "expires_at": time.time() + hours * 3600,
            "temporary": str(temporary),
        }
        (logs / "preview.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"url": record["url"], "readonly": True}), flush=True)
        deadline = time.monotonic() + hours * 3600
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(1)
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        server.shutdown()
        server.server_close()
        connection.close()
        if (
            temporary.parent.resolve() == (ROOT / ".tmp/frontend-workspace").resolve()
            and not temporary.is_symlink()
        ):
            shutil.rmtree(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=2)
    args = parser.parse_args()
    if not 0 < args.hours <= 8:
        parser.error("Preview duration must be within eight hours")
    main(args.hours)
