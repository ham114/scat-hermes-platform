"""Apply one reviewed configuration proposal while owned services are stopped."""

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def maintenance_gate(targets):
    from hermes_runtime.skills import checked_path

    live = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=scat-hermes-h7b",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    if live:
        raise ValueError("stop_owned_core_containers_before_applying")
    state = subprocess.run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if state.stdout.strip() not in {"inactive", "failed"}:
        raise ValueError("learning_scheduler_not_confirmed_stopped")
    # Missing/unreadable gates fail closed; no database/schema creation.
    if set(targets["gates"]) != {"tasks", "bindings", "trials", "learning"}:
        raise ValueError("all_idle_gates_required")
    queries = {
        "tasks": "SELECT count(*) FROM model_tasks WHERE state IN ('queued','claimed','unknown')",
        "bindings": "SELECT count(*) FROM runtime_admissions",
        "trials": "SELECT count(*) FROM trials WHERE state='running'",
        "learning": "SELECT count(*) FROM automatic_skill_jobs WHERE state IN ('reserved','authoring','unknown','trial_running')",
    }
    for name, path in targets["gates"].items():
        with sqlite3.connect(
            "file:" + checked_path(ROOT, path).as_posix() + "?mode=ro", uri=True
        ) as db:
            if db.execute(queries[name]).fetchone()[0]:
                raise ValueError("active_or_unresolved_work")


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing_Anaconda_required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--proposal", required=True)
    args = parser.parse_args()
    import json

    from hermes_runtime.agent_settings import AgentSettingsStore

    os.umask(0o077)
    print(
        json.dumps(AgentSettingsStore(ROOT, args.manifest).apply(args.proposal, maintenance_gate))
    )


if __name__ == "__main__":
    main()
