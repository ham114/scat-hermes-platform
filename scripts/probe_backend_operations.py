"""Actual isolated restart/failure tests; no external LLM requests or business payload export."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    import httpx

    from backend_ops.environment import dump
    from backend_ops.runtime import APP, DEFAULTS, Stack, http_ok, matches
    from hermes_runtime.registry import RunRegistry
    from hermes_runtime.results import ToolResultStore

    if sys.executable != APP:
        raise ValueError("existing_server_conda_required")
    identity = uuid4().hex[:8]
    work = ROOT / ".tmp/h7a" / ("probe-" + identity)
    work.mkdir(parents=True)
    config_path = work / "config.json"
    dump(config_path, json.dumps(DEFAULTS | {"instance": "h7a-" + identity}))
    relative = config_path.relative_to(ROOT).as_posix()
    stack = Stack(ROOT, relative)
    report = {"status": "running", "checks": {}, "llm_calls": 0, "production_gpu": False}
    checks = report["checks"]
    log_path = ROOT / "var/validation" / ("20261024-H7A-" + identity + "-lifecycle.log")
    report_path = ROOT / "docs/validation" / ("20261024-H7A-" + identity + "-lifecycle.json")
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(work),
        "TMP": str(work),
        "TEMP": str(work),
    }

    def command(action, *, success=True):
        with log_path.open("ab") as log:
            log_path.chmod(0o600)
            result = subprocess.run(
                [APP, "-B", str(ROOT / "scripts/manage_backend.py"), action, "--config", relative],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                timeout=170,
                check=False,
            )
        if success:
            assert result.returncode == 0, "operation_" + action + "_failed"
        return result.returncode

    def wait_for(predicate, timeout=45):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.5)
        raise AssertionError("lifecycle_condition_timeout")

    try:
        command("start")
        checks["actual_three_service_start"] = stack.status()["status"] == "ready"
        checks["duplicate_start_refused"] = command("start", success=False) != 0
        key = (stack.home / "platform-auth.key").read_text().strip()
        origin = f"http://127.0.0.1:{stack.config['platform_port']}"
        with httpx.Client(
            base_url=origin, headers={"Authorization": "Bearer " + key}, trust_env=False, timeout=10
        ) as client:
            response = client.post("/api/v1/hermes/threads", json={})
            response.raise_for_status()
            thread = response.json()["thread_id"]
            other = client.post("/api/v1/hermes/threads", json={}).json()["thread_id"]
            registry = RunRegistry(stack.home / "platform.sqlite", workspace_root=ROOT)
            context = registry.require_thread("scat-researcher", thread)
            run = "run_" + uuid4().hex
            registry.bind(context, str(uuid4()), run)
            store = ToolResultStore(stack.home / "results.sqlite", workspace_root=ROOT)
            payload = {"status": "ok", "data": {"synthetic": True, "count": 1}}
            reference = store.put(
                context.scope_key, run, "restart-fixture", "search_scat_flights", payload
            )
            route = f"/api/v1/hermes/threads/{thread}/runs/{run}/results/{reference['result_id']}"
            checks["synthetic_receipt_before_restart"] = (
                client.get(route).json()["result"] == payload
            )
            cross = route.replace(thread, other)
            checks["cross_thread_read_refused"] = client.get(cross).status_code == 404
        # Dependency unhealthy is distinct from process death; do not restart or claim readiness.
        record = stack.record()
        native = next(r["identity"] for r in record["children"] if r["name"] == "hermes")
        assert matches(native)
        os.kill(native["pid"], signal.SIGSTOP)
        try:
            wait_for(lambda: stack.status().get("status") == "degraded", timeout=25)
            checks["unhealthy_is_degraded_without_restart"] = (
                stack.status()["controller_alive"]
                and matches(native)
                and http_ok(stack.config["platform_port"], "/health/live")
                and not http_ok(stack.config["platform_port"], "/health/ready")
            )
        finally:
            if matches(native):
                os.kill(native["pid"], signal.SIGCONT)
        wait_for(lambda: stack.status()["status"] == "ready")
        checks["dependency_recovery_ready"] = True
        command("stop")
        checks["stop_preserves_databases"] = all(
            (stack.home / name).is_file()
            for name in ("platform.sqlite", "results.sqlite", "jobs.sqlite")
        )
        command("start")
        with httpx.Client(
            base_url=origin, headers={"Authorization": "Bearer " + key}, trust_env=False, timeout=10
        ) as client:
            checks["authenticated_thread_restored"] = thread in {
                r["thread_id"] for r in client.get("/api/v1/hermes/threads").json()["items"]
            }
            checks["synthetic_receipt_restored"] = client.get(route).json()["result"] == payload
            checks["wrong_key_refused"] = (
                client.get(
                    "/api/v1/hermes/threads", headers={"Authorization": "Bearer invalid"}
                ).status_code
                == 401
            )
        # Actual child exit causes a fail-closed stop; no blind automatic restarts.
        record = stack.record()
        native = next(r["identity"] for r in record["children"] if r["name"] == "hermes")
        assert matches(native)
        os.kill(native["pid"], signal.SIGKILL)
        wait_for(lambda: not stack.status()["controller_alive"])
        checks["child_exit_stops_stack"] = stack.status()["status"] == "failed" and all(
            not matches(r["identity"]) for r in record["children"]
        )
        command("start")
        # Actual supervisor death cannot authorize a second stack; verified orphan stop is required.
        record = stack.record()
        assert stack.controller_matches(record)
        os.kill(record["controller"]["pid"], signal.SIGKILL)
        wait_for(lambda: not stack.controller_matches(record))
        checks["controller_death_detected"] = stack.status()["status"] == "stale"
        checks["orphan_restart_refused"] = command("start", success=False) != 0
        command("stop")
        checks["verified_orphan_stop"] = all(not matches(r["identity"]) for r in record["children"])
        checks["candidate_key_private"] = (
            stack.home / "platform-auth.key"
        ).stat().st_mode & 0o077 == 0
        assert all(checks.values()), "one_or_more_lifecycle_checks_failed"
        report["status"] = "passed"
    except Exception as error:  # noqa: BLE001 -- the probe persists only fixed safe failure metadata.
        report.update(status="failed", error_type=type(error).__name__)
        state = stack.record()
        if state:
            report["service_failure_state"] = {
                key: state.get(key) for key in ("status", "reason", "checks")
            }
        # These are only this synthetic instance's service logs, retained privately on the server.
        for name in ("hermes", "platform", "worker"):
            source = stack.home / (name + ".log")
            if source.is_file() and not source.is_symlink():
                target = (
                    ROOT / "var/validation" / ("20261024-H7A-" + identity + "-" + name + ".log")
                )
                shutil.copyfile(source, target)
                target.chmod(0o600)
        # Only fixed assertion codes produced by this synthetic probe may be reported.
        if isinstance(error, AssertionError):
            report["error_code"] = str(error)[:100]
    finally:
        with contextlib.suppress(Exception):
            stack.stop()
        ports_closed = True
        for port in (18120, 18121, 18122):
            with socket.socket() as sock:
                ports_closed &= sock.connect_ex(("127.0.0.1", port)) != 0
        report["ports_closed"] = ports_closed

        async def cleanup_database():
            from pymongo import AsyncMongoClient

            from core import settings

            client = AsyncMongoClient(settings.FLIGHT_MONGO_URI.get_secret_value())
            try:
                database = client[settings.FLIGHT_MONGO_DB]
                suffix = ("h7a-" + identity).replace("-", "_")
                for name in ("analysis_runs_" + suffix, "analysis_artifacts_" + suffix):
                    await database.drop_collection(name)
            finally:
                await client.close()

        try:
            asyncio.run(cleanup_database())
            report["isolated_collections_removed"] = True
        except Exception as error:  # noqa: BLE001 -- retain fixed failure metadata even if cleanup fails.
            report.update(status="failed", cleanup_error_type=type(error).__name__)
        assert stack.home.parent == ROOT / "var/backend" and not stack.home.is_symlink()
        if ports_closed:
            shutil.rmtree(stack.home, ignore_errors=True)
            shutil.rmtree(stack.temporary, ignore_errors=True)
            shutil.rmtree(work)
        report["owned_state_removed"] = not stack.home.exists()
        dump(report_path, json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": report_path.relative_to(ROOT).as_posix(),
                "status": report["status"],
                "checks": checks,
            }
        )
    )
    return 0 if report["status"] == "passed" and ports_closed else 1


if __name__ == "__main__":
    raise SystemExit(main())
