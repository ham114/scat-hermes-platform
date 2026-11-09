"""Inspect learning or close an uncertain job after independently checking no process is live."""

import argparse
import asyncio
import json
import os
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    import fcntl

    from hermes_runtime.authoring_store import AuthoringStore
    from hermes_runtime.autonomy_config import load_autonomy
    from hermes_runtime.experience_learning import LearningLedger

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "close-unknown", "retry-failed"])
    parser.add_argument("--job")
    args = parser.parse_args()
    policy = load_autonomy(ROOT).learning
    ledger = LearningLedger(ROOT, policy)
    if args.action == "status":
        print(json.dumps(ledger.status(policy.owner), ensure_ascii=False))
        return
    if not args.job or not re.fullmatch("[a-f0-9]{32}", args.job):
        parser.error("close-unknown requires a job ID")
    with (ledger.path.parent / "learning.lock").open("a") as lock:
        # Stop the scheduler first; another model dispatch must not race this check.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 18144)) == 0:
                raise ValueError("automatic_author_still_listening")
        expected_home = ".tmp/autonomy/author-" + args.job
        for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                arguments = cmdline.read_bytes().split(b"\x00")
            except (OSError, PermissionError):
                continue
            if (
                expected_home.encode() in arguments
                or str(ROOT / expected_home).encode() in arguments
            ):
                raise ValueError("automatic_author_process_still_live")
        job = ledger.get(args.job)
        if args.action == "retry-failed":
            from run_auto_skill_learning import execute

            from hermes_runtime.skill_learning import SkillLearningStore

            if (
                job["candidate"]
                and SkillLearningStore(AuthoringStore(ROOT, policy.author_state)).get(
                    policy.owner, job["candidate"]
                )["draft"]
            ):
                raise ValueError("existing_draft_must_be_reviewed_without_regeneration")
            child = ledger.retry_failed(args.job)
            print(
                json.dumps(
                    {"retry_of": args.job, "new_job": child["id"], "daily_slot_charged": True}
                ),
                flush=True,
            )
            asyncio.run(execute(ledger, child, policy))
            print(
                json.dumps({"job": child["id"], "state": ledger.get(child["id"])["state"]}),
                flush=True,
            )
            return
        if job["state"] != "unknown":
            raise ValueError("only_unknown_jobs_can_be_closed")
        if job["trial"]:
            trial = AuthoringStore(ROOT, policy.author_state).trial(policy.owner, job["trial"])
            if trial["state"] == "running":
                raise ValueError("trial_requires_existing_interrupted_trial_recovery")
        ledger.update(args.job, "failed", error="operator_closed_unknown_without_replay")
        print(json.dumps({"job": args.job, "state": "failed", "requests_replayed": 0}))


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("existing_Anaconda_required")
    os.umask(0o077)
    main()
