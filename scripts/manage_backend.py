"""Manage a finite, synthetic, loopback-only backend candidate in existing Conda."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from backend_ops.runtime import APP, Stack, preflight

    if sys.platform != "linux" or sys.executable != APP:
        raise ValueError("use_existing_server_scat_agent")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "start", "serve", "status", "stop"])
    parser.add_argument("--config", default="config/backend-h7a.json")
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    stack = Stack(ROOT, args.config)
    if args.action == "preflight":
        print(json.dumps(preflight(ROOT, stack.config)))
    elif args.action == "status":
        print(json.dumps(stack.status()))
    elif args.action == "stop":
        print(json.dumps(stack.stop()))
    elif args.action == "serve":
        if (
            not args.token
            or len(args.token) != 32
            or not all(c in "0123456789abcdef" for c in args.token)
        ):
            raise ValueError("invalid_controller_token")
        stack.serve(args.token)
    else:
        if stack.controller_matches(stack.record()):
            raise ValueError("already_running")
        preflight(ROOT, stack.config)
        token = uuid4().hex
        controller = subprocess.Popen(
            [
                APP,
                "-B",
                str(Path(__file__).resolve()),
                "serve",
                "--config",
                str(stack.path.relative_to(ROOT)),
                "--token",
                token,
            ],
            cwd=ROOT,
            env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        until = time.monotonic() + stack.config["startup_seconds"]
        while time.monotonic() < until:
            if controller.poll() is not None:
                raise ValueError("candidate_controller_exited")
            row = stack.record()
            if row and row.get("token") == token:
                if row["status"] == "ready":
                    print(json.dumps(stack.status()))
                    return 0
                if row["status"] in {"failed", "stopped"}:
                    raise ValueError("candidate_start_failed")
            time.sleep(0.5)
        # Never leave a timed-out candidate running behind a failed start command.
        row = stack.record()
        if row and row.get("token") == token:
            stack.stop()
        elif controller.poll() is None:
            controller.terminate()
            controller.wait(timeout=10)
        raise ValueError("candidate_start_timed_out")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        allowed = {
            "already_running",
            "candidate_controller_exited",
            "candidate_start_failed",
            "candidate_start_timed_out",
            "controller_stop_timed_out",
            "disk_admission_refused",
            "orphan_services_require_stop",
            "unverified_orphan_process",
        }
        code = (
            str(error)
            if isinstance(error, ValueError) and str(error) in allowed
            else "backend_operation_failed"
        )
        print(
            json.dumps({"status": "failed", "code": code, "errno": getattr(error, "errno", None)})
        )
        raise SystemExit(1) from None
