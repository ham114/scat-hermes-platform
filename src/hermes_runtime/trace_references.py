"""Protect planner evidence while any referencing approved/pending job is unresolved."""

import json
import sqlite3
from contextlib import closing

from hermes_runtime.skills import checked_path
from hermes_runtime.trace_lifecycle import sha


def track_parent(root, scope, parent, state_path):
    if not parent:
        return
    trace = checked_path(root, parent["path"])
    state = checked_path(root, state_path.relative_to(root).as_posix())
    with closing(sqlite3.connect(f"file:{trace.as_posix()}?mode=rw", uri=True)) as db, db:
        # Pre-S6 databases have no safely known completion state; already protected.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_trace_runs'").fetchone():
            return
        db.execute("""CREATE TABLE IF NOT EXISTS skill_trace_references (
            scope TEXT NOT NULL, run_id TEXT NOT NULL, state_file TEXT NOT NULL,
            PRIMARY KEY(scope,run_id,state_file))""")
        db.execute(
            "INSERT OR IGNORE INTO skill_trace_references VALUES(?,?,?)",
            (scope, parent["run_id"], state.relative_to(root).as_posix()),
        )


def referenced(db, root, scope, run, now):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_trace_references'").fetchone():
        return False
    refs = db.execute(
        "SELECT state_file FROM skill_trace_references WHERE scope=? AND run_id=?", (scope, run)
    ).fetchall()
    for (name,) in refs:
        if root is None:
            return True
        try:
            path = checked_path(root, name)
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as state:
                jobs = state.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='skill_jobs'"
                ).fetchone()
                query = (
                    "SELECT t.state,t.expires,t.snapshot,t.sha256"
                    + (",j.state" if jobs else ",NULL")
                    + " FROM model_tasks t"
                    + (" LEFT JOIN skill_jobs j USING(task_id)" if jobs else "")
                    + " WHERE t.scope=? AND json_extract(t.snapshot,'$.skills.parent_budget.run_id')=?"
                )
                rows = state.execute(query, (scope, run)).fetchall()
                for task_state, expiry, body, checksum, job_state in rows:
                    if sha(json.loads(body)) != checksum:
                        return True
                    if (job_state or task_state) not in {
                        "succeeded",
                        "partial",
                        "failed",
                        "cancelled",
                        "expired",
                    } and not (job_state is None and task_state == "pending" and expiry <= now):
                        return True
        except (OSError, ValueError, sqlite3.Error):
            return True
    return False
