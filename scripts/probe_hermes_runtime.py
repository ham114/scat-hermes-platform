"""Bounded real-model smoke; owns and stops only its child Hermes process."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx
from pydantic import SecretStr

from application.context import ExecutionContext
from hermes_runtime.client import HermesHTTPRuntime
from hermes_runtime.registry import RunRegistry


async def probe(args, report):
    mode = "mcp-synthetic" if args.with_mcp else "text"
    identity = uuid4().hex[:12]
    home = ROOT / "var/hermes" / f"probe-{mode}-{identity}"
    report.update(mode=mode, profile=str(home.relative_to(ROOT)))
    log_path = ROOT / "var/validation" / f"hermes-{mode}-{identity}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / ".tmp/hermes-probe"
    temporary.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1", "HERMES_HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
    }
    command = [str(Path("/home/h3c/anaconda3/envs/scat-hermes/bin/python")),
               "-B", str(ROOT / "scripts/run_hermes_api.py"),
               "--home", str(home.relative_to(ROOT)), "--port", str(args.port)]
    if args.with_mcp:
        command += ["--with-mcp", "--synthetic-mcp"]
    origin = f"http://127.0.0.1:{args.port}"
    async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
        try:
            await health.get(origin + "/health")
        except httpx.TransportError:
            pass
        else:
            raise RuntimeError("probe_port_already_in_use")
    with log_path.open("w") as log:
        log_path.chmod(0o600)
        child = await asyncio.to_thread(
            subprocess.Popen, command, cwd=ROOT, env=env, stdout=log, stderr=log
        )
        report.update(log=str(log_path.relative_to(ROOT)), child_pid=child.pid)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as health:
                for _ in range(90):
                    if child.poll() is not None:
                        raise RuntimeError("hermes_exited_before_health")
                    try:
                        response = await health.get(origin + "/health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("hermes_health_timeout")
            report["health"] = "ok"
            key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
            registry = RunRegistry(home / "platform-run-bindings.sqlite", workspace_root=ROOT)
            context = ExecutionContext("scat-researcher", "probe-" + identity)
            prompt = (
                "Call search_scat_flights exactly once with callsign TEST123 and limit 1. "
                "This is an isolated synthetic protocol test, not real flight data. "
                "Then reply with one short sentence stating whether the result is synthetic."
                if args.with_mcp else "Reply only with OK."
            )
            async with HermesHTTPRuntime(origin, SecretStr(key), registry) as runtime:
                run = await runtime.create_run(context, prompt, request_id=uuid4())
                counts = Counter()
                tools = set()
                final_output = ""
                tool_errors = 0
                terminal = None
                async with asyncio.timeout(180):
                    async for event in runtime.events(context, run["run_id"]):
                        counts[event.name] += 1
                        if event.name.startswith("tool."):
                            name = event.payload.get("tool")
                            if isinstance(name, str):
                                tools.add(name)
                        if event.name == "tool.completed" and event.payload.get("error"):
                            tool_errors += 1
                        if event.name == "run.completed":
                            final_output = event.payload.get("output", "")
                        if event.name.startswith("run."):
                            terminal = event.name
                status = await runtime.get_run(context, run["run_id"])
                report.update(run_id=run["run_id"], terminal=terminal,
                              run_status=status.get("status"), events=dict(counts),
                              tools=sorted(tools), assistant_text_present=bool(final_output), tool_errors=tool_errors,
                              output_ok=final_output.strip() == "OK" if isinstance(final_output, str) else False)
                if terminal != "run.completed" or status.get("status") != "completed":
                    raise RuntimeError("run_did_not_complete")
                if args.with_mcp and (
                    "mcp__scat__search_scat_flights" not in tools
                    or counts["tool.completed"] < 1 or counts["tool.failed"] or tool_errors
                ):
                    raise RuntimeError("expected_tool_not_completed")
                if not args.with_mcp and not report["output_ok"]:
                    raise RuntimeError("unexpected_text_response")
                if args.with_mcp:
                    report["output_labels_synthetic"] = (
                        isinstance(final_output, str) and "synthetic" in final_output.lower()
                    )
                    if not report["output_labels_synthetic"]:
                        raise RuntimeError("synthetic_result_not_labelled")
                report["status"] = "passed"
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
            report["child_stopped"] = child.poll() is not None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-mcp", action="store_true")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    report = {"status": "running", "started_at": time.time()}
    try:
        asyncio.run(probe(args, report))
    except (RuntimeError, OSError, TimeoutError, ValueError, httpx.HTTPError) as exc:
        # Never include exception messages, headers, response bodies or credentials.
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, RuntimeError) and str(exc) in {
            "probe_port_already_in_use", "hermes_exited_before_health", "hermes_health_timeout",
            "run_did_not_complete", "expected_tool_not_completed",
            "unexpected_text_response", "synthetic_result_not_labelled",
        }:
            report["error_code"] = str(exc)
    report["finished_at"] = time.time()
    name = "mcp" if args.with_mcp else "text"
    path = ROOT / f"var/validation/hermes-{name}-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
