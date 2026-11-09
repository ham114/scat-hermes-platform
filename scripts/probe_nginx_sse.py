"""Probe an isolated Nginx using timed synthetic SSE; never reload host Nginx."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "var/validation/h4-nginx-sse.json"


async def probe(report):
    prefix = ROOT / ".tmp/h4" / ("nginx-" + uuid4().hex[:12])
    prefix.mkdir(parents=True)
    for name in ["client", "proxy", "fastcgi", "uwsgi", "scgi"]:
        (prefix / name).mkdir()
    report["prefix"] = str(prefix.relative_to(ROOT))
    config = f"""pid {prefix}/nginx.pid;
error_log {prefix}/error.log warn;
worker_processes 1;
events {{ worker_connections 32; }}
http {{
 access_log off;
 client_body_temp_path {prefix}/client;
 proxy_temp_path {prefix}/proxy;
 fastcgi_temp_path {prefix}/fastcgi;
 uwsgi_temp_path {prefix}/uwsgi;
 scgi_temp_path {prefix}/scgi;
 server {{
  listen 127.0.0.1:18082;
  location /api/v1/hermes/ {{
   proxy_pass http://127.0.0.1:18083;
   proxy_http_version 1.1;
   proxy_set_header Connection "";
   proxy_set_header Last-Event-ID $http_last_event_id;
   proxy_buffering off;
   proxy_cache off;
   proxy_read_timeout 180s;
   proxy_pass_header X-Accel-Buffering;
  }}
 }}
}}"""
    (prefix / "nginx.conf").write_text(config)
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            seen["last_event_id"] = self.headers.get("Last-Event-ID")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b'data: {"event":"probe.started"}\n\n')
            self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(b'data: {"event":"probe.completed"}\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 18083), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    child = None
    try:
        command = ["/usr/sbin/nginx", "-p", str(prefix) + "/", "-c", str(prefix / "nginx.conf")]
        checked = await asyncio.to_thread(
            subprocess.run, command + ["-t"], capture_output=True, check=False
        )
        report["configuration_valid"] = checked.returncode == 0
        if checked.returncode:
            raise RuntimeError("nginx_config_rejected")
        log_path = ROOT / "var/validation/h4-nginx-process.log"
        with log_path.open("w") as log:
            child = await asyncio.create_subprocess_exec(
                *command, "-g", "daemon off;", stdout=log, stderr=log, start_new_session=True
            )
            report["child_pid"] = child.pid
            await asyncio.sleep(0.4)
            if child.returncode is not None:
                raise RuntimeError("isolated_nginx_exited")
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                started = time.monotonic()
                received = []
                async with client.stream(
                    "GET",
                    "http://127.0.0.1:18082/api/v1/hermes/probe-events",
                    headers={"Last-Event-ID": "7"},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            received.append(
                                (round(time.monotonic() - started, 3), json.loads(line[6:]))
                            )
            report.update(
                first_event_seconds=received[0][0],
                last_event_seconds=received[-1][0],
                last_event_id_forwarded=seen.get("last_event_id") == "7",
                events=[item[1]["event"] for item in received],
            )
            assert (
                len(received) == 2
                and received[0][0] < 1.2
                and received[-1][0] - received[0][0] >= 1.3
            )
            assert report["last_event_id_forwarded"]
            report["status"] = "passed"
    finally:
        if child is not None and child.returncode is None:
            child.send_signal(signal.SIGQUIT)
            try:
                await asyncio.wait_for(child.wait(), timeout=5)
            except TimeoutError:
                if os.getpgid(child.pid) == child.pid:
                    os.killpg(child.pid, signal.SIGKILL)
                await child.wait()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=3)
        report["child_stopped"] = child is None or child.returncode is not None
        report["upstream_stopped"] = not thread.is_alive()


if __name__ == "__main__":
    report = {"status": "running", "upstream": "timed synthetic SSE", "started_at": time.time()}
    try:
        asyncio.run(probe(report))
    except Exception as exc:  # noqa: BLE001 -- no raw logs or credentials are emitted.
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, RuntimeError) and str(exc) in {
            "nginx_config_rejected",
            "isolated_nginx_exited",
        }:
            report["error_code"] = str(exc)
    report["finished_at"] = time.time()
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
