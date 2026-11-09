"""Optional loopback S5 preview services; separate state, finite lifetime, no GPU."""

import argparse
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"
HERMES = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


async def main(hours):
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    for port in (18080, 18081, 18099):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    home = ROOT / "var/demo/s5"
    home.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / ".tmp/s5-demo"
    temporary.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    env = os.environ | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "HERMES_BASE_URL": "http://127.0.0.1:18081",
        "HERMES_STATE_FILE": str(home / "platform.sqlite"),
        "HERMES_TASK_STATE_FILE": str(home / "jobs.sqlite"),
        "HERMES_RESULT_STATE_FILE": str(home / "results.sqlite"),
        "HERMES_SKILL_JOBS_ENABLED": "true",
        "HERMES_WORKFLOWS_ENABLED": "true",
        "SCAT_S5_WORKFLOWS": "1",
        "SCAT_S5_SYNTHETIC": "1",
        "ANALYSIS_IDENTITY_MODE": "single_user",
        "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
        "ANALYSIS_RUNS_COLLECTION": "analysis_runs_s5_demo",
        "ANALYSIS_ARTIFACTS_COLLECTION": "analysis_artifacts_s5_demo",
        "SCAT_GBAR_V23_ENABLED": "false",
        "SCAT_GBAR_V23_COMPARISON_SUBPROCESS_ENABLED": "false",
        "SCAT_JOB_TEMP_DIR": str(temporary / "worker"),
    }
    commands = [
        (
            "native",
            [
                HERMES,
                "-B",
                str(ROOT / "scripts/run_hermes_api.py"),
                "--home",
                str((home / "main").relative_to(ROOT)),
                "--port",
                "18081",
                "--with-mcp",
                "--synthetic-mcp",
                "--with-model-tasks",
                "--with-skills",
                "--with-workflows",
            ],
        ),
        ("api", [APP, "-B", str(ROOT / "scripts/run_platform_api.py"), "--port", "18080"]),
        (
            "worker",
            [
                APP,
                "-B",
                str(ROOT / "scripts/run_model_worker.py"),
                "--job-home",
                str((home / "jobs").relative_to(ROOT)),
                "--background-port",
                "18099",
            ],
        ),
    ]
    children = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    record = {
        "controller_pid": os.getpid(),
        "started_at": time.time(),
        "expires_at": time.time() + hours * 3600,
        "synthetic": True,
        "children": [],
    }
    try:
        for name, command in commands:
            path = home / (name + ".log")
            with path.open("a", encoding="utf-8") as stream:
                path.chmod(0o600)
                child = await asyncio.to_thread(
                    subprocess.Popen,
                    command,
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=stream,
                )
            children.append(child)
            record["children"].append({"name": name, "pid": child.pid})
        (home / "services.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        until = time.monotonic() + hours * 3600
        while time.monotonic() < until and not stop.is_set():
            if any(child.poll() is not None for child in children):
                raise RuntimeError("Preview service exited; inspect var/demo/s5 logs")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=2)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
        from application.skill_cpu_backend import process_start, request_stop
        from application.skill_job_factory import create_skill_tasks

        if (home / "jobs.sqlite").exists():
            tasks = create_skill_tasks(
                ROOT, home / "jobs.sqlite", workflows_enabled=True, workflow_synthetic=True
            )
            with tasks.store.connect() as db:
                jobs = db.execute(
                    "SELECT t.scope,t.task_id FROM model_tasks t JOIN skill_jobs j USING(task_id)"
                ).fetchall()
            for row in jobs:
                tasks.skills.store.cancel(row["scope"], row["task_id"])
                request_stop(tasks.skills.store, row["task_id"])

        def owned():
            found = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdecimal() or int(entry.name) == os.getpid():
                    continue
                try:
                    if entry.stat().st_uid != os.getuid():
                        continue
                    args = (entry / "cmdline").read_bytes().split(b"\0")
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                prefixes = (str(home).encode(), str(home.relative_to(ROOT)).encode())
                if any(
                    arg == prefix or arg.startswith(prefix + b"/")
                    for arg in args
                    for prefix in prefixes
                ):
                    found.append((int(entry.name), process_start(int(entry.name))))
            return found

        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid, birth in owned():
                if birth and process_start(pid) == birth:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, sig)
            for _ in range(30):
                if not owned():
                    break
                await asyncio.sleep(0.1)
        assert not owned(), "Preview descendants still active"
        assert (
            temporary.resolve() == (ROOT / ".tmp/s5-demo").resolve() and not temporary.is_symlink()
        )
        shutil.rmtree(temporary)
        record["stopped_at"] = time.time()
        (home / "services.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=4)
    args = parser.parse_args()
    if not 0 < args.hours <= 8:
        parser.error("Preview duration must be at most 8 hours")
    asyncio.run(main(args.hours))
