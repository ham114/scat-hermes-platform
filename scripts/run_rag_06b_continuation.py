"""Continue only unfinished cases under a recorded prospective allocation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-06b"
PYTHON = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def main():
    import fcntl

    if sys.executable != PYTHON:
        raise ValueError("use_existing_scat_hermes")
    allocation = json.loads((ROOT / "services/rag/06b-continuation-allocation.json").read_text())
    if (
        allocation["maximum_total_calls"] != 1600
        or allocation["maximum_total_conservative_tokens"]
        > allocation["corrected_conservative_ceiling"]
    ):
        raise ValueError("invalid_cumulative_allocation")
    if any(
        hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != digest
        for p, digest in allocation["source_runtime"].items()
    ):
        raise ValueError("runtime_changed_since_primary_cases")
    target = OUT / "continuation.json"
    with (OUT / "continuation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if target.exists():
            raise ValueError("continuation_exists_no_replay")
        state = {
            "status": "running",
            "allocation": allocation,
            "jobs": {},
            "started_at": time.time(),
        }

        def save():
            temp = target.with_suffix(".write")
            temp.write_text(json.dumps(state, indent=2))
            os.replace(temp, target)

        waiting, active = list(allocation["jobs"]), {}
        save()
        try:
            while waiting or active:
                if time.time() - state["started_at"] > 7200:
                    raise TimeoutError("continuation_deadline")
                while waiting and len(active) < 2:
                    job = waiting.pop(0)
                    folder = ROOT / job["output"]
                    if folder.exists():
                        raise ValueError("existing_job_not_replayed")
                    log = (OUT / ("continuation-" + str(job["id"]) + ".log")).open("w")
                    process = subprocess.Popen(
                        [
                            PYTHON,
                            "-B",
                            "scripts/run_real_agent_rag.py",
                            "--config",
                            job["config"],
                            "--batch",
                            "--output",
                            str(folder),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    active[job["id"]] = (process, log, folder)
                    state["jobs"][str(job["id"])] = {"status": "running", "pid": process.pid}
                    save()
                for identity, (process, log, folder) in list(active.items()):
                    if process.poll() is None:
                        continue
                    log.close()
                    summary = (
                        json.loads((folder / "summary.json").read_text())
                        if (folder / "summary.json").exists()
                        else {}
                    )
                    state["jobs"][str(identity)].update(
                        status=summary.get("status", "failed_process"),
                        cases=len(summary.get("cases", [])),
                        exit=process.returncode,
                        budget=summary.get("budget"),
                    )
                    del active[identity]
                    save()
                time.sleep(2)
            state["status"] = "completed"
            save()
        finally:
            for process, log, _folder in active.values():
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                log.close()


if __name__ == "__main__":
    main()
