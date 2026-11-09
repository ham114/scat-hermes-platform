"""Durable confirmation and single-worker outbox, separate from chat lifetimes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4


class TaskNotFound(LookupError):
    pass


class TaskConflict(ValueError):
    pass


def canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class TaskStore:
    def __init__(self, path: Path, *, workspace_root: Path, clock=time.time):
        self.path = path.resolve()
        if self.path == workspace_root.resolve() or not self.path.is_relative_to(
            workspace_root.resolve()
        ):
            raise ValueError("Task state must remain inside the project")
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as c, c:
            c.execute("""CREATE TABLE IF NOT EXISTS model_tasks (
                task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, proposal_key TEXT NOT NULL,
                run_id TEXT UNIQUE NOT NULL, snapshot TEXT NOT NULL, sha256 TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                updated REAL NOT NULL, worker TEXT, lease REAL, error_code TEXT,
                UNIQUE(scope, proposal_key))""")
            c.execute(
                "CREATE INDEX IF NOT EXISTS ix_model_tasks_scope ON model_tasks(scope,created)"
            )
        self.path.chmod(0o600)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def _recover(self, c):
        c.execute(
            "UPDATE model_tasks SET state='unknown', error_code='worker_lease_expired', updated=? "
            "WHERE state='claimed' AND lease<=?",
            (self.clock(), self.clock()),
        )

    def _decode(self, row):
        data = dict(row)
        snapshot = json.loads(data.pop("snapshot"))
        if digest(snapshot) != data["sha256"]:
            raise TaskConflict("Stored task integrity check failed")
        data["snapshot"] = snapshot
        if data["state"] == "pending" and data["expires"] <= self.clock():
            data["state"] = "expired"
        return data

    def prepare(self, scope, proposal_key, snapshot, *, ttl=900):
        if not re.fullmatch(r"scat:[0-9a-f]{64}", scope) or not 1 <= len(proposal_key) <= 200:
            raise ValueError("Invalid task scope or proposal key")
        encoded = canonical(snapshot)
        if len(encoded.encode()) > 262144:
            raise ValueError("Task snapshot exceeds limit")
        sha = digest(snapshot)
        now = self.clock()
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM model_tasks WHERE scope=? AND proposal_key=?", (scope, proposal_key)
            ).fetchone()
            if row:
                # Creation timestamps/run IDs are allocated once, outside caller idempotency data.
                old = self._decode(row)
                if digest(old["snapshot"]["proposal"]) != digest(snapshot["proposal"]):
                    raise TaskConflict("Proposal changed under the same request ID")
                return old
            count = c.execute(
                "SELECT count(*) FROM model_tasks WHERE scope=? AND state='pending' AND expires>?",
                (scope, now),
            ).fetchone()[0]
            if count >= 50:
                raise TaskConflict("Too many pending tasks")
            c.execute(
                "INSERT INTO model_tasks VALUES (?,?,?,?,?,?,'pending',?,?,?,NULL,NULL,NULL)",
                (
                    uuid4().hex,
                    scope,
                    proposal_key,
                    snapshot["run"]["run_id"],
                    encoded,
                    sha,
                    now,
                    now + ttl,
                    now,
                ),
            )
            return self._decode(
                c.execute(
                    "SELECT * FROM model_tasks WHERE scope=? AND proposal_key=?",
                    (scope, proposal_key),
                ).fetchone()
            )

    def get(self, scope, task_id):
        with closing(self.connect()) as c, c:
            self._recover(c)
            row = c.execute(
                "SELECT * FROM model_tasks WHERE scope=? AND task_id=?", (scope, task_id)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task not found")
            return self._decode(row)

    def list(self, scope, *, limit=50):
        if not 1 <= limit <= 50:
            raise ValueError("Invalid limit")
        with closing(self.connect()) as c, c:
            self._recover(c)
            rows = c.execute(
                "SELECT * FROM model_tasks WHERE scope=? ORDER BY created DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
            return [self._decode(row) for row in rows]

    def confirm(self, scope, task_id, sha256):
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM model_tasks WHERE scope=? AND task_id=?", (scope, task_id)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task not found")
            data = self._decode(row)
            if sha256 != data["sha256"]:
                raise TaskConflict("Confirmed parameters changed")
            if data["state"] in {"queued", "claimed", "finished", "unknown"}:
                return data
            if data["state"] != "pending":
                raise TaskConflict("Confirmation expired or revoked")
            c.execute(
                "UPDATE model_tasks SET state='queued',updated=? WHERE task_id=?",
                (self.clock(), task_id),
            )
            return self._decode(
                c.execute("SELECT * FROM model_tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def revoke(self, scope, task_id):
        with closing(self.connect()) as c, c:
            changed = c.execute(
                "UPDATE model_tasks SET state='revoked',updated=? WHERE scope=? AND task_id=? AND state IN ('pending','queued')",
                (self.clock(), scope, task_id),
            ).rowcount
            if changed != 1:
                raise TaskConflict("Only pending or unclaimed tasks can be revoked")
        return self.get(scope, task_id)

    def claim(self, worker, *, lease_seconds=60):
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            self._recover(c)
            if c.execute(
                "SELECT 1 FROM model_tasks WHERE state IN ('claimed','unknown') LIMIT 1"
            ).fetchone():
                return None
            row = c.execute(
                "SELECT * FROM model_tasks WHERE state='queued' AND json_type(snapshot,'$.skills') IS NULL ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            data = self._decode(row)
            c.execute(
                "UPDATE model_tasks SET state='claimed',worker=?,lease=?,updated=? WHERE task_id=?",
                (worker, self.clock() + lease_seconds, self.clock(), data["task_id"]),
            )
            return self._decode(
                c.execute(
                    "SELECT * FROM model_tasks WHERE task_id=?", (data["task_id"],)
                ).fetchone()
            )

    def heartbeat(self, task_id, worker, *, lease_seconds=60):
        with closing(self.connect()) as c, c:
            n = c.execute(
                "UPDATE model_tasks SET lease=?,updated=? WHERE task_id=? AND worker=? AND state='claimed' AND lease>?",
                (self.clock() + lease_seconds, self.clock(), task_id, worker, self.clock()),
            ).rowcount
            if n != 1:
                raise TaskConflict("Worker no longer owns the task")

    def finish(self, task_id, worker, *, unknown=False, error_code=None):
        with closing(self.connect()) as c, c:
            n = c.execute(
                "UPDATE model_tasks SET state=?,error_code=?,updated=? WHERE task_id=? AND worker=? AND state='claimed' AND lease>?",
                (
                    "unknown" if unknown else "finished",
                    error_code,
                    self.clock(),
                    task_id,
                    worker,
                    self.clock(),
                ),
            ).rowcount
            if n != 1:
                raise TaskConflict("Worker completion requires reconciliation")

    def reconcile_terminal(self, task_id, *, run_id):
        """Called only after the adapter has verified an existing terminal Mongo run."""
        with closing(self.connect()) as c, c:
            n = c.execute(
                "UPDATE model_tasks SET state='finished',error_code=NULL,updated=? WHERE task_id=? AND run_id=? AND state='unknown'",
                (self.clock(), task_id, run_id),
            ).rowcount
            if n != 1:
                raise TaskConflict("Task is not awaiting reconciliation")
