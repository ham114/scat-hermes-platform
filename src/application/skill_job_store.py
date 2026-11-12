"""Approved skill jobs, lease fencing and delegated capabilities in the task outbox DB."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from uuid import uuid4

from application.task_store import TaskConflict, TaskNotFound, TaskStore, canonical


class SkillJobStore:
    def __init__(self, tasks: TaskStore):
        self.tasks, self.path, self.clock = tasks, tasks.path, tasks.clock
        with closing(tasks.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_jobs (
                    task_id TEXT PRIMARY KEY, state TEXT NOT NULL, worker TEXT,
                    generation INTEGER NOT NULL DEFAULT 0, lease REAL,
                    budget_revision INTEGER NOT NULL DEFAULT 0,
                    parent_imported INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL, error TEXT, result_reference TEXT);
                CREATE TABLE IF NOT EXISTS skill_job_delegations (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, parent TEXT,
                    scope TEXT NOT NULL, operations TEXT NOT NULL, depth INTEGER NOT NULL,
                    expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS skill_job_sessions (
                    id TEXT PRIMARY KEY, delegation TEXT NOT NULL, task_id TEXT NOT NULL,
                    scope TEXT NOT NULL, native_run TEXT NOT NULL, native_session TEXT NOT NULL,
                    worker TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
                    UNIQUE(native_session, native_run));
                CREATE TABLE IF NOT EXISTS skill_job_budget_revisions (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, limits TEXT NOT NULL,
                    at REAL NOT NULL, PRIMARY KEY(task_id,revision));
                CREATE TABLE IF NOT EXISTS skill_backend_executions (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_key TEXT NOT NULL,
                    generation INTEGER NOT NULL, worker TEXT NOT NULL,
                    state TEXT NOT NULL, request TEXT NOT NULL, request_sha TEXT NOT NULL,
                    output TEXT, output_sha TEXT, pid INTEGER, process_start TEXT,
                    error TEXT, started REAL, finished REAL, calls INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(task_id,step_key));
                CREATE TABLE IF NOT EXISTS skill_job_events (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, at REAL NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL);
            """)

    @contextmanager
    def transaction(self):
        with closing(self.tasks.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def event(self, db, task_id, kind, **data):
        db.execute(
            "INSERT INTO skill_job_events VALUES(NULL,?,?,?,?)",
            (task_id, self.clock(), kind, canonical(data)),
        )

    def get(self, scope, task_id):
        row = self.tasks.get(scope, task_id)
        with self.transaction() as db:
            expired = db.execute(
                "SELECT 1 FROM skill_jobs WHERE task_id=? AND state='running' AND lease<=?",
                (task_id, self.clock()),
            ).fetchone()
            if expired:
                db.execute(
                    "UPDATE skill_jobs SET state='unknown',error='lease_expired',updated=? WHERE task_id=?",
                    (self.clock(), task_id),
                )
                self.event(db, task_id, "lease_expired")
            job = db.execute("SELECT * FROM skill_jobs WHERE task_id=?", (task_id,)).fetchone()
        return {**row, "job": dict(job) if job else None}

    def confirm(self, scope, task_id, sha):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM model_tasks WHERE task_id=? AND scope=?", (task_id, scope)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task not found")
            row = self.tasks._decode(row)
            if row["sha256"] != sha:
                raise TaskConflict("Approved snapshot changed")
            if row["state"] not in {"pending", "queued", "claimed", "unknown", "finished"}:
                raise TaskConflict("Task expired or revoked")
            if "skills" not in row["snapshot"]:
                raise TaskConflict("Skill job snapshot required")
            job = db.execute("SELECT * FROM skill_jobs WHERE task_id=?", (task_id,)).fetchone()
            if job is None:
                if row["state"] != "pending":
                    raise TaskConflict("Task is not pending approval")
                db.execute(
                    "INSERT INTO skill_jobs(task_id,state,updated) VALUES(?,'queued',?)",
                    (task_id, self.clock()),
                )
                db.execute(
                    "INSERT INTO skill_job_budget_revisions VALUES(?,0,?,?)",
                    (task_id, canonical(row["snapshot"]["skills"]["budget"]), self.clock()),
                )
                db.execute(
                    "UPDATE model_tasks SET state='queued',updated=? WHERE task_id=?",
                    (self.clock(), task_id),
                )
                self.event(db, task_id, "approved", snapshot_sha256=sha)
        return self.get(scope, task_id)

    def fence(self, db, task_id, worker, generation, revision=None, *, allow_cancel=False):
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM skill_jobs WHERE task_id=?", (task_id,)).fetchone()
        if (
            row is None
            or row["state"]
            not in ({"running", "cancel_requested"} if allow_cancel else {"running"})
            or row["worker"] != worker
            or row["generation"] != generation
            or row["lease"] <= self.clock()
        ):
            raise TaskConflict("skill_job_lease_lost")
        if revision is not None and row["budget_revision"] != revision:
            raise TaskConflict("skill_job_budget_revision_changed")
        return dict(row)

    def require_lease(self, task_id, worker, generation, revision=None):
        with closing(self.tasks.connect()) as db:
            return self.fence(db, task_id, worker, generation, revision)

    def claim(self, worker, *, concurrency=1, lease_seconds=60):
        if not worker or not 1 <= concurrency <= 16 or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid skill worker admission")
        with self.transaction() as db:
            expired = db.execute(
                "SELECT task_id FROM skill_jobs WHERE state='running' AND lease<=?",
                (self.clock(),),
            ).fetchall()
            for row in expired:
                db.execute(
                    "UPDATE skill_jobs SET state='unknown',error='lease_expired',updated=? WHERE task_id=?",
                    (self.clock(), row[0]),
                )
                db.execute(
                    "UPDATE model_tasks SET state='unknown',error_code='skill_job_lease_expired' WHERE task_id=?",
                    (row[0],),
                )
                self.event(db, row[0], "lease_expired")
            count = db.execute(
                "SELECT count(*) FROM model_tasks WHERE state IN ('claimed','unknown')"
            ).fetchone()[0]
            if count >= concurrency:
                return None
            row = db.execute(
                "SELECT t.* FROM model_tasks t JOIN skill_jobs j USING(task_id) WHERE j.state='queued' AND t.state='queued' AND j.parent_imported=1 ORDER BY t.created LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            data = self.tasks._decode(row)
            db.execute(
                "UPDATE skill_jobs SET state='running',worker=?,generation=generation+1,lease=?,updated=?,error=NULL WHERE task_id=?",
                (worker, self.clock() + lease_seconds, self.clock(), data["task_id"]),
            )
            db.execute(
                "UPDATE model_tasks SET state='claimed',worker=?,lease=?,updated=?,error_code=NULL WHERE task_id=?",
                (worker, self.clock() + lease_seconds, self.clock(), data["task_id"]),
            )
            self.event(db, data["task_id"], "claimed", worker=worker)
        return self.get(data["scope"], data["task_id"])

    def heartbeat(self, task_id, worker, generation, *, lease_seconds=60):
        with self.transaction() as db:
            self.fence(db, task_id, worker, generation, allow_cancel=True)
            db.execute(
                "UPDATE skill_jobs SET lease=?,updated=? WHERE task_id=?",
                (self.clock() + lease_seconds, self.clock(), task_id),
            )
            db.execute(
                "UPDATE model_tasks SET lease=?,updated=? WHERE task_id=?",
                (self.clock() + lease_seconds, self.clock(), task_id),
            )

    def state(
        self,
        scope,
        task_id,
        state,
        *,
        error=None,
        reference=None,
        worker=None,
        generation=None,
        expected_generation=None,
    ):
        """Trusted reconciliation/control operation. Never exposed as a model tool."""
        if state not in {
            "queued",
            "paused",
            "unknown",
            "succeeded",
            "partial",
            "failed",
            "cancelled",
        }:
            raise ValueError("Invalid skill job state")
        with self.transaction() as db:
            if worker is not None:
                self.fence(db, task_id, worker, generation)
            row = db.execute(
                "SELECT * FROM model_tasks WHERE task_id=? AND scope=?", (task_id, scope)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task not found")
            current = db.execute("SELECT * FROM skill_jobs WHERE task_id=?", (task_id,)).fetchone()
            if current is None or current["state"] in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
            }:
                raise TaskConflict("Skill job already terminal")
            if expected_generation is not None and current["generation"] != expected_generation:
                raise TaskConflict("Skill job changed during reconciliation")
            if current["state"] == "cancel_requested" and state != "cancelled":
                raise TaskConflict("Skill job cancellation is pending")
            db.execute(
                "UPDATE skill_jobs SET state=?,generation=generation+1,worker=NULL,lease=NULL,updated=?,error=?,result_reference=COALESCE(?,result_reference) WHERE task_id=?",
                (state, self.clock(), error, canonical(reference) if reference else None, task_id),
            )
            task_state = (
                "finished"
                if state in {"succeeded", "partial", "failed", "cancelled"}
                else (
                    "unknown"
                    if state == "unknown"
                    else ("queued" if state == "queued" else "revoked")
                )
            )
            db.execute(
                "UPDATE model_tasks SET state=?,worker=NULL,lease=NULL,updated=?,error_code=? WHERE task_id=?",
                (task_state, self.clock(), error, task_id),
            )
            self.event(db, task_id, state, error=error, reference=reference)

    def delegate(self, scope, task_id, child_scope, operations, *, parent=None, max_depth=6):
        row = self.get(scope, task_id)
        allowed = set(row["snapshot"]["skills"]["operations"])
        depth = 1
        with self.transaction() as db:
            if row["job"] is None or row["job"]["state"] in {
                "cancel_requested",
                "cancelled",
                "succeeded",
                "partial",
                "failed",
            }:
                raise TaskConflict("Job is not delegable")
            if parent:
                p = db.execute(
                    "SELECT * FROM skill_job_delegations WHERE id=? AND task_id=? AND revoked=0",
                    (parent, task_id),
                ).fetchone()
                if p is None or p["expires"] <= self.clock():
                    raise TaskConflict("Invalid parent delegation")
                allowed &= set(json.loads(p["operations"]))
                depth = p["depth"] + 1
            if not operations or set(operations) - allowed or depth > max_depth:
                raise TaskConflict("Delegation exceeds approved capability")
            if (
                db.execute(
                    "SELECT count(*) FROM skill_job_delegations WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                >= 32
            ):
                raise TaskConflict("Delegation capacity reached")
            identity = uuid4().hex
            db.execute(
                "INSERT INTO skill_job_delegations VALUES(?,?,?,?,?,?,?,0)",
                (
                    identity,
                    task_id,
                    parent,
                    child_scope,
                    canonical(sorted(operations)),
                    depth,
                    row["snapshot"]["skills"]["expires"],
                ),
            )
            self.event(
                db,
                task_id,
                "delegated",
                delegation=identity,
                parent=parent,
                operations=sorted(operations),
            )
        return identity

    def bind(self, delegation, scope, native_run, native_session, worker, generation):
        with self.transaction() as db:
            d = db.execute(
                "SELECT * FROM skill_job_delegations WHERE id=? AND scope=? AND revoked=0",
                (delegation, scope),
            ).fetchone()
            if d is None or d["expires"] <= self.clock():
                raise TaskConflict("Invalid delegated scope")
            job = self.fence(db, d["task_id"], worker, generation)
            identity = uuid4().hex
            db.execute(
                "INSERT INTO skill_job_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    delegation,
                    d["task_id"],
                    scope,
                    native_run,
                    native_session,
                    worker,
                    generation,
                    job["budget_revision"],
                ),
            )
        return identity

    def session(self, identity, *, operation=None):
        with closing(self.tasks.connect()) as db:
            row = db.execute(
                "SELECT s.*,d.operations,d.expires,d.revoked FROM skill_job_sessions s JOIN skill_job_delegations d ON s.delegation=d.id WHERE s.id=?",
                (identity,),
            ).fetchone()
            if (
                row is None
                or row["revoked"]
                or row["expires"] <= self.clock()
                or (operation and operation not in json.loads(row["operations"]))
            ):
                raise TaskConflict("Invalid delegated session")
            parent = db.execute(
                "SELECT parent FROM skill_job_delegations WHERE id=?", (row["delegation"],)
            ).fetchone()[0]
            while parent:
                ancestor = db.execute(
                    "SELECT * FROM skill_job_delegations WHERE id=? AND revoked=0", (parent,)
                ).fetchone()
                if ancestor is None or ancestor["expires"] <= self.clock():
                    raise TaskConflict("Delegation ancestor revoked")
                parent = ancestor["parent"]
            self.fence(db, row["task_id"], row["worker"], row["generation"], row["revision"])
            return dict(row)

    def cancel(self, scope, task_id):
        with self.transaction() as db:
            task = db.execute(
                "SELECT * FROM model_tasks WHERE task_id=? AND scope=?", (task_id, scope)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task not found")
            self.tasks._decode(task)
            job = db.execute("SELECT state FROM skill_jobs WHERE task_id=?", (task_id,)).fetchone()
            if job is None:
                raise TaskConflict("No approved skill job")
            if job["state"] not in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "cancel_requested",
            }:
                db.execute(
                    "UPDATE skill_jobs SET state='cancel_requested',generation=generation+1,updated=? WHERE task_id=?",
                    (self.clock(), task_id),
                )
                db.execute("UPDATE skill_job_delegations SET revoked=1 WHERE task_id=?", (task_id,))
                self.event(db, task_id, "cancel_requested")
        return self.get(scope, task_id)

    def limits(self, task_id):
        with closing(self.tasks.connect()) as db:
            row = db.execute(
                "SELECT r.limits FROM skill_job_budget_revisions r JOIN skill_jobs j ON r.task_id=j.task_id AND r.revision=j.budget_revision WHERE j.task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise TaskNotFound("Job budget not found")
            return json.loads(row[0])
