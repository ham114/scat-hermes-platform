"""Independent visualization lane in the existing local task database.

No model_tasks/skill_jobs rows or legacy recovery rules are changed. This initial
foundation deliberately provides no success/publication operation: only a future
verified publication transaction may complete a job successfully.
"""

import hashlib
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from application.context import ExecutionContext
from visualization_runtime.contracts import ExecutionLimits, VisualizationRequest
from visualization_runtime.json_codec import (
    canonical as canonical,  # noqa: PLC0414 -- public compatibility export
)
from visualization_runtime.paths import confined_path


class JobNotFound(LookupError):
    pass


class JobConflict(ValueError):
    pass


_TRANSITIONS = {
    "generating": {"rendering"},
    "rendering": {"reviewing", "revising"},
    "reviewing": {"revising", "publishing"},
    "revising": {"rendering"},
}


class VisualizationJobStore:
    def __init__(self, path: Path, *, workspace_root: Path, clock=time.time, limits=None):
        self.path = confined_path(workspace_root, path)
        self.workspace_root = workspace_root.resolve()
        self.clock = clock
        self.limits = limits or ExecutionLimits()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS visualization_schema(version INTEGER PRIMARY KEY)"
            )
            versions = [r[0] for r in db.execute("SELECT version FROM visualization_schema")]
            if versions and versions not in ([1], [2], [3], [4], [5]):
                raise JobConflict("unsupported_visualization_schema")
            schema = """
                CREATE TABLE IF NOT EXISTS visualization_jobs(
                    task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, thread TEXT NOT NULL,
                    request_id TEXT NOT NULL, request TEXT NOT NULL, request_sha TEXT NOT NULL,
                    state TEXT NOT NULL, phase TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                    worker TEXT, lease_until REAL, created REAL NOT NULL, updated REAL NOT NULL,
                    error_code TEXT, UNIQUE(owner,thread,request_id));
                CREATE INDEX IF NOT EXISTS ix_visualization_jobs_claim
                    ON visualization_jobs(state,created);
                CREATE INDEX IF NOT EXISTS ix_visualization_jobs_owner
                    ON visualization_jobs(owner,state);
                CREATE TABLE IF NOT EXISTS visualization_events(
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    kind TEXT NOT NULL, at REAL NOT NULL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            from visualization_runtime.persistence import migrate_foundation

            migrate_foundation(db)
            db.execute(
                "INSERT OR IGNORE INTO visualization_job_budgets "
                "SELECT task_id,?,created+? FROM visualization_jobs",
                (self.limits.model_dump_json(), self.limits.wall_seconds),
            )
            db.execute("DELETE FROM visualization_schema")
            db.execute("INSERT INTO visualization_schema VALUES(5)")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _event(self, db, row, kind):
        db.execute(
            "INSERT INTO visualization_events VALUES(NULL,?,?,?,?)",
            (row["task_id"], row["generation"], kind, self.clock()),
        )

    @staticmethod
    def _decode(row):
        value = dict(row)
        raw = value.pop("request")
        if hashlib.sha256(raw.encode()).hexdigest() != value["request_sha"]:
            raise JobConflict("visualization_request_integrity_failed")
        value["request"] = VisualizationRequest.model_validate_json(raw)
        return value

    def _recover(self, db):
        db.execute(
            "UPDATE visualization_jobs SET state='failed',error_code='visualization_deadline_exceeded',"
            "updated=? WHERE state='queued' AND task_id IN "
            "(SELECT task_id FROM visualization_job_budgets WHERE deadline<=?)",
            (self.clock(), self.clock()),
        )
        rows = db.execute(
            "SELECT * FROM visualization_jobs WHERE state='running' AND lease_until<=?",
            (self.clock(),),
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE visualization_jobs SET state='needs_reconciliation',"
                "error_code='lease_expired',updated=? WHERE task_id=?",
                (self.clock(), row["task_id"]),
            )
            self._event(db, row, "lease_expired")
            from visualization_runtime.persistence import fence_pending_calls

            fence_pending_calls(db, row["task_id"], self.clock())

    def create(
        self,
        context: ExecutionContext,
        request_id: str,
        request: VisualizationRequest,
        *,
        parent=None,
        admission_sha=None,
        preference_setup=None,
        context_setup=None,
    ):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("invalid_visualization_request_id")
        raw = canonical(request.model_dump(mode="json"))
        sha = hashlib.sha256(raw.encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM visualization_jobs WHERE owner=? AND thread=? AND request_id=?",
                (context.user_id, context.thread_id, request_id),
            ).fetchone()
            if row:
                decoded = self._decode(row)
                if admission_sha is not None:
                    admitted = db.execute(
                        "SELECT input_sha FROM visualization_admissions WHERE task_id=?",
                        (row["task_id"],),
                    ).fetchone()
                    if admitted:
                        if admitted[0] != admission_sha:
                            raise JobConflict("visualization_request_id_conflict")
                        return decoded
                if row["request_sha"] != sha and decoded["request"] != request:
                    raise JobConflict("visualization_request_id_conflict")
                return decoded
            task_id, now = uuid4().hex, self.clock()
            from visualization_runtime.parent_budget import reserve

            reserve(db, self, context, task_id, parent)
            if preference_setup is not None:
                request = preference_setup(db, task_id, request)
            if context_setup is not None:
                request = context_setup(db, task_id, request)
            if preference_setup is not None or context_setup is not None:
                raw = canonical(request.model_dump(mode="json"))
                sha = hashlib.sha256(raw.encode()).hexdigest()
            if admission_sha is not None:
                db.execute(
                    "INSERT INTO visualization_admissions VALUES(?,?)", (task_id, admission_sha)
                )
            db.execute(
                "INSERT INTO visualization_jobs(task_id,owner,thread,request_id,request,"
                "request_sha,state,phase,created,updated) VALUES(?,?,?,?,?,?,'queued',"
                "'queued',?,?)",
                (task_id, context.user_id, context.thread_id, request_id, raw, sha, now, now),
            )
            row = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            self._event(db, row, "queued")
            db.execute(
                "INSERT INTO visualization_job_budgets VALUES(?,?,?)",
                (task_id, self.limits.model_dump_json(), now + self.limits.wall_seconds),
            )
            return self._decode(row)

    def get(self, context: ExecutionContext, task_id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=? AND owner=? AND thread=?",
                (task_id, context.user_id, context.thread_id),
            ).fetchone()
            if row is None:
                raise JobNotFound("visualization_job_not_found")
            self._recover(db)
            return self._decode(
                db.execute(
                    "SELECT * FROM visualization_jobs WHERE task_id=?", (task_id,)
                ).fetchone()
            )

    def claim(self, worker: str, *, concurrency=2, per_owner=1, lease_seconds=60):
        if not isinstance(worker, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", worker):
            raise ValueError("invalid_visualization_worker")
        if (
            type(concurrency) is not int
            or not 1 <= concurrency <= 8
            or type(per_owner) is not int
            or not 1 <= per_owner <= concurrency
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= 300
        ):
            raise ValueError("invalid_visualization_admission")
        with self.transaction() as db:
            self._recover(db)
            # Unknown resources retain their slot until a reconciliation layer
            # proves cleanup, but never consume the legacy model queue's slots.
            active = "('running','needs_reconciliation')"
            if (
                db.execute(
                    f"SELECT count(*) FROM visualization_jobs WHERE state IN {active}"
                ).fetchone()[0]
                >= concurrency
            ):
                return None
            row = db.execute(
                "SELECT j.* FROM visualization_jobs j WHERE j.state='queued' AND "
                f"(SELECT count(*) FROM visualization_jobs a WHERE a.owner=j.owner AND a.state IN {active})<? "
                "ORDER BY j.created,j.task_id LIMIT 1",
                (per_owner,),
            ).fetchone()
            if row is None:
                return None
            self._decode(row)
            db.execute(
                "UPDATE visualization_jobs SET state='running',"
                "phase=COALESCE(resume_phase,'generating'),resume_phase=NULL,worker=?,"
                "generation=generation+1,lease_until=?,updated=? WHERE task_id=?",
                (worker, self.clock() + lease_seconds, self.clock(), row["task_id"]),
            )
            row = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=?", (row["task_id"],)
            ).fetchone()
            self._event(db, row, "claimed")
            return self._decode(row)

    def _fence(self, db, task_id, worker, generation):
        row = db.execute(
            "SELECT * FROM visualization_jobs WHERE task_id=? AND worker=? "
            "AND generation=? AND state='running' AND lease_until>?",
            (task_id, worker, generation, self.clock()),
        ).fetchone()
        if row is None:
            raise JobConflict("visualization_lease_lost")
        return row

    def heartbeat(self, task_id, worker, generation, *, lease_seconds=60):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid_visualization_lease")
        with self.transaction() as db:
            self._fence(db, task_id, worker, generation)
            db.execute(
                "UPDATE visualization_jobs SET lease_until=?,updated=? WHERE task_id=?",
                (self.clock() + lease_seconds, self.clock(), task_id),
            )

    def transition(self, task_id, worker, generation, *, expected_phase, phase):
        with self.transaction() as db:
            row = self._fence(db, task_id, worker, generation)
            if row["phase"] != expected_phase or phase not in _TRANSITIONS.get(
                expected_phase, set()
            ):
                raise JobConflict("invalid_visualization_phase_transition")
            db.execute(
                "UPDATE visualization_jobs SET phase=?,updated=? WHERE task_id=?",
                (phase, self.clock(), task_id),
            )
            self._event(db, row, phase)

    def fail(self, task_id, worker, generation, *, error_code):
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code):
            raise ValueError("invalid_visualization_error_code")
        with self.transaction() as db:
            row = self._fence(db, task_id, worker, generation)
            active = (
                db.execute(
                    "SELECT 1 FROM visualization_resources WHERE task_id=? AND state='active'",
                    (task_id,),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM visualization_provider_calls WHERE task_id=? AND state='sending'",
                    (task_id,),
                ).fetchone()
            )
            db.execute(
                "UPDATE visualization_jobs SET state=?,lease_until=NULL,resume_phase=?,"
                "error_code=?,updated=? WHERE task_id=?",
                (
                    "needs_reconciliation" if active else "failed",
                    "failed" if active else None,
                    error_code,
                    self.clock(),
                    task_id,
                ),
            )
            from visualization_runtime.persistence import fence_pending_calls

            fence_pending_calls(db, task_id, self.clock())
            self._event(db, row, "failed")

    def cancel(self, context: ExecutionContext, task_id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=? AND owner=? AND thread=?",
                (task_id, context.user_id, context.thread_id),
            ).fetchone()
            if row is None:
                raise JobNotFound("visualization_job_not_found")
            if row["state"] == "queued":
                state = "cancelled"
            elif row["state"] in {"running", "needs_reconciliation"}:
                # Keep the resource slot until a future worker confirms cleanup.
                if row["error_code"] == "user_cancelled":
                    return self._decode(row)
                state = "needs_reconciliation"
            else:
                return self._decode(row)
            db.execute(
                "UPDATE visualization_jobs SET state=?,generation=generation+1,"
                "lease_until=NULL,error_code='user_cancelled',updated=? WHERE task_id=?",
                (state, self.clock(), task_id),
            )
            row = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            self._event(db, row, "cancel_requested")
            from visualization_runtime.persistence import fence_pending_calls

            fence_pending_calls(db, task_id, self.clock())
            return self._decode(row)
