"""Four preregistered ten-case shards, at most two active, no hidden replay."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-06b"
PYTHON = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"


def main():
    import fcntl
    import sys

    if sys.executable != PYTHON:
        raise ValueError("use_existing_scat_hermes")
    os.umask(0o077)
    with (OUT / "campaign.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = OUT / "campaign-v3.json"
        if target.exists():
            raise ValueError("campaign_exists_inspect_before_any_resume")
        sources = [
            "src/hermes_runtime/real_rag.py",
            "src/hermes_runtime/real_rag_native.py",
            "scripts/run_real_agent_rag.py",
            "scripts/run_rag_06b_campaign_v3.py",
            "scripts/serve_qwen3_06b.py",
            *[f"services/rag/real-agent-06b-v3-shard-{i}.json" for i in range(1, 5)],
        ]
        state = {
            "status": "running",
            "started_at": time.time(),
            "sources": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
            "shards": {},
            "max_parallel_shards": 2,
            "total_model_request_ceiling": 1600,
            "total_conservative_token_ceiling": 35000000,
            "negative_suite_reserved_requests": 80,
            "negative_suite_reserved_tokens": 1500000,
        }

        def save():
            temporary = target.with_suffix(".write")
            temporary.write_text(json.dumps(state, indent=2))
            os.replace(temporary, target)

        save()
        active = {}
        waiting = list(range(1, 5))
        failed = False
        try:
            while active or waiting:
                if time.time() - state["started_at"] > 4 * 3600:
                    raise TimeoutError("campaign_wall_budget")
                while not failed and waiting and len(active) < 2:
                    number = waiting.pop(0)
                    folder = ROOT / "var/validation" / f"20261026-rag06b-v3-dev40-shard-{number}"
                    if folder.exists():
                        raise ValueError("shard_exists_no_replay")
                    log = (OUT / f"shard-{number}.log").open("w")
                    process = subprocess.Popen(
                        [
                            PYTHON,
                            "-B",
                            "scripts/run_real_agent_rag.py",
                            "--config",
                            f"services/rag/real-agent-06b-v3-shard-{number}.json",
                            "--batch",
                            "--output",
                            str(folder),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    active[number] = (process, log, folder)
                    state["shards"][str(number)] = {
                        "status": "running",
                        "pid": process.pid,
                        "output": str(folder),
                    }
                    save()
                for number, (process, log, folder) in list(active.items()):
                    if process.poll() is None:
                        continue
                    log.close()
                    summary_path = folder / "summary.json"
                    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
                    complete = len(summary.get("cases", [])) == 10 and all(
                        c["modes"][m]["status"] == "passed"
                        for c in summary.get("cases", [])
                        for m in ("fast", "deep")
                    )
                    state["shards"][str(number)].update(
                        status="completed" if complete else "execution_failure",
                        exit=process.returncode,
                        cases=len(summary.get("cases", [])),
                        budget=summary.get("budget"),
                    )
                    failed |= not complete
                    del active[number]
                    save()
                if failed and not active:
                    break
                time.sleep(2)
            state["status"] = "stopped_after_execution_failure" if failed else "completed"
            state["unstarted_shards"] = waiting
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
