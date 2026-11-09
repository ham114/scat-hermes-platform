"""Fixed Compose service entrypoints with existing Conda environments and child cleanup."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role",
        choices=["platform", "main", "actions", "worker", "job-control", "author-api", "author"],
    )
    role = parser.parse_args().role
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Conda image required")
    temporary = ROOT / ".tmp" / ("compose-" + role)
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        TMPDIR=str(temporary),
        TMP=str(temporary),
        TEMP=str(temporary),
        HOME=str(temporary),
        XDG_CACHE_HOME=str(temporary / "cache"),
    )
    homes = "var/deploy/h7b/shared/homes/"
    commands = {
        "platform": ["run_platform_api.py", "--port", "18120"],
        "actions": ["run_private_actions.py"],
        "job-control": ["run_job_control.py"],
        "worker": [
            "run_model_worker.py",
            "--job-home",
            homes + "jobs",
            "--background-port",
            "18128",
        ],
        "main": [
            "run_hermes_api.py",
            "--profile",
            "scat-main",
            "--home",
            homes + "main",
            "--port",
            "18121",
            "--with-mcp",
            "--with-skills",
            "--with-model-tasks",
            "--with-workflows",
            "--skills-policy",
            "config/rag-readonly-policy.json",
            "--rag-config",
            "services/rag/real-agent-production-v1.json",
        ],
        "author-api": ["run_skill_author_api.py", "--port", "18124"],
        "author": [
            "run_hermes_api.py",
            "--profile",
            "scat-author",
            "--home",
            homes + "author",
            "--port",
            "18125",
            "--author-api-url",
            "http://127.0.0.1:18124",
        ],
    }
    if role == "main" and os.environ.get("SCAT_READONLY_BUDGET_JSON"):
        # The native policy validates that these deployment settings only lower
        # its ceilings; model arguments cannot change this environment value.
        commands["main"] += ["--readonly-budget", os.environ["SCAT_READONLY_BUDGET_JSON"]]
    if role != "worker":
        os.execv(
            sys.executable,
            [sys.executable, "-B", "scripts/" + commands[role][0], *commands[role][1:]],
        )
    child = subprocess.Popen(
        [sys.executable, "-B", "scripts/" + commands[role][0], *commands[role][1:]], cwd=ROOT
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            alive = child.poll() is None
            body = json.dumps(
                {
                    "status": "alive" if alive else "failed",
                    "service": "worker",
                    "readiness_is_not_task_completion": True,
                }
            ).encode()
            self.send_response(200 if alive else 503)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 18126), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: child.terminate() if child.poll() is None else None)
    try:
        code = child.wait()
    finally:
        server.shutdown()
        server.server_close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
