"""Transactional cold receipts in the same private SQLite durability boundary.

Unknown/pre-migration runs and unfinished dispatches are protected. This module
never infers that a crashed worker or an external computation has stopped.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from uuid import uuid4

from hermes_runtime.skills import SkillPolicyError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class TraceLifecycle:
    def __init__(self, path, policy, *, clock=time.time, workspace_root=None):
        self.path, self.policy, self.clock = path, policy, clock
        self.root = workspace_root
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_trace_runs (
                    scope TEXT NOT NULL, run_id TEXT NOT NULL,
                    state TEXT NOT NULL, finished_at REAL, held INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope,run_id));
                CREATE TABLE IF NOT EXISTS skill_trace_archives (
                    archive_id TEXT PRIMARY KEY, scope TEXT NOT NULL, run_id TEXT NOT NULL,
                    finished_at REAL NOT NULL, archived_at REAL NOT NULL,
                    policy_sha256 TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS skill_archive_scope
                    ON skill_trace_archives(scope,run_id);
                CREATE TABLE IF NOT EXISTS skill_trace_maintenance (
                    batch_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                    policy_sha256 TEXT NOT NULL, archived_records INTEGER NOT NULL,
                    purged_archives INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_trace_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL);
            """)
            old = db.execute("SELECT policy FROM skill_trace_policy WHERE id=1").fetchone()
            if old:
                trace = json.loads(old[0])["trace"]
                if any(
                    getattr(policy.trace, key) < trace[key]
                    for key in ("hot_retention_days", "archive_retention_days")
                ):
                    raise SkillPolicyError("skill_retention_preview_required")
            db.execute(
                "INSERT OR REPLACE INTO skill_trace_policy VALUES(1,?)",
                (encoded(policy.model_dump()),),
            )

    def bind(self, scope, run):
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute(
                "SELECT 1 FROM skill_trace_archives WHERE scope=? AND run_id=?", (scope, run)
            ).fetchone():
                raise SkillPolicyError("archived_skill_run_requires_new_run")
            db.execute(
                """INSERT INTO skill_trace_runs(scope,run_id,state) VALUES(?,?,'active')
                ON CONFLICT(scope,run_id) DO UPDATE SET state='active',finished_at=NULL""",
                (scope, run),
            )

    def finish(self, scope, run):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE skill_trace_runs SET state='finished',finished_at=? WHERE scope=? AND run_id=?",
                (self.clock(), scope, run),
            )

    def hold(self, scope, run, held=True):
        """Trusted operator/reference holder only; never exposed as an Agent tool."""
        with closing(sqlite3.connect(self.path)) as db, db:
            if (
                db.execute(
                    "UPDATE skill_trace_runs SET held=? WHERE scope=? AND run_id=?",
                    (int(held), scope, run),
                ).rowcount
                != 1
            ):
                raise SkillPolicyError("skill_trace_run_not_found")

    @staticmethod
    def tables(db):
        return [
            name
            for name in ("skill_loads", "skill_operations")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        ]

    def count(self, db):
        return sum(db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in self.tables(db))

    def _eligible(self, db):
        from hermes_runtime.trace_references import referenced

        rows = db.execute(
            "SELECT scope,run_id,finished_at FROM skill_trace_runs WHERE state='finished' AND held=0 ORDER BY finished_at"
        ).fetchall()
        tables = self.tables(db)
        for scope, run, finished in rows:
            if referenced(db, self.root, scope, run, self.clock()):
                continue
            if (
                "skill_operations" in tables
                and db.execute(
                    """SELECT 1 FROM skill_operations WHERE scope=? AND run_id=?
                AND (finished_at IS NULL OR outcome NOT IN ('returned','dispatch_error'))""",
                    (scope, run),
                ).fetchone()
            ):
                continue
            yield scope, run, finished

    def maintain(self, db, *, force=False):
        """Caller holds BEGIN IMMEDIATE. Archive+verify+hot deletion commit together."""
        now = self.clock()
        count = self.count(db)
        threshold = max(
            1, int(self.policy.trace.max_hot_records * self.policy.trace.archive_high_watermark)
        )
        moved = purged = 0
        for scope, run, finished in self._eligible(db):
            if (
                not force
                and count < threshold
                and now - finished < self.policy.trace.hot_retention_days * 86400
            ):
                continue
            payload = {}
            for table in self.tables(db):
                cursor = db.execute(
                    f"SELECT * FROM {table} WHERE scope=? AND run_id=? ORDER BY id", (scope, run)
                )
                columns = [c[0] for c in cursor.description]
                payload[table] = [dict(zip(columns, row, strict=True)) for row in cursor]
            size = sum(len(rows) for rows in payload.values())
            if not size:
                continue
            archive_id = uuid4().hex
            body = encoded(payload)
            checksum = sha(payload)
            db.execute(
                "INSERT INTO skill_trace_archives VALUES(?,?,?,?,?,?,?,?)",
                (archive_id, scope, run, finished, now, self.policy.sha256, body, checksum),
            )
            stored = db.execute(
                "SELECT payload,sha256 FROM skill_trace_archives WHERE archive_id=?", (archive_id,)
            ).fetchone()
            if stored != (body, checksum) or sha(json.loads(stored[0])) != checksum:
                raise SkillPolicyError("skill_archive_integrity")
            for table in payload:
                db.execute(f"DELETE FROM {table} WHERE scope=? AND run_id=?", (scope, run))
            moved += size
            count -= size
            if moved >= self.policy.trace.max_hot_records:
                break
        for archive_id, payload, checksum in db.execute(
            """SELECT a.archive_id,a.payload,a.sha256 FROM skill_trace_archives a
            JOIN skill_trace_runs r ON a.scope=r.scope AND a.run_id=r.run_id
            WHERE r.state='finished' AND r.held=0 AND a.finished_at<?""",
            (now - self.policy.trace.archive_retention_days * 86400,),
        ).fetchall():
            binding = db.execute(
                "SELECT scope,run_id FROM skill_trace_archives WHERE archive_id=?", (archive_id,)
            ).fetchone()
            from hermes_runtime.trace_references import referenced

            if referenced(db, self.root, *binding, now):
                continue
            if sha(json.loads(payload)) != checksum:
                raise SkillPolicyError("skill_archive_integrity")
            db.execute("DELETE FROM skill_trace_archives WHERE archive_id=?", (archive_id,))
            purged += 1
        if moved or purged:
            db.execute(
                "INSERT INTO skill_trace_maintenance VALUES(?,?,?,?,?)",
                (uuid4().hex, now, self.policy.sha256, moved, purged),
            )
        db.execute(
            "DELETE FROM skill_trace_maintenance WHERE created_at<?",
            (now - self.policy.trace.archive_retention_days * 86400,),
        )
        for scope, run, finished in list(self._eligible(db)):
            if now - finished <= self.policy.trace.archive_retention_days * 86400:
                continue
            if any(
                db.execute(
                    f"SELECT 1 FROM {table} WHERE scope=? AND run_id=?", (scope, run)
                ).fetchone()
                for table in self.tables(db)
            ):
                continue
            if not db.execute(
                "SELECT 1 FROM skill_trace_archives WHERE scope=? AND run_id=?", (scope, run)
            ).fetchone():
                db.execute("DELETE FROM skill_trace_runs WHERE scope=? AND run_id=?", (scope, run))
        return {"archived_records": moved, "purged_archives": purged, "hot_records": count}

    def reserve(self, db, *, policy=None):
        if policy is not None and policy.sha256 != self.policy.sha256:
            if any(
                getattr(policy.trace, key) < getattr(self.policy.trace, key)
                for key in ("hot_retention_days", "archive_retention_days")
            ):
                raise SkillPolicyError("skill_retention_preview_required")
            self.policy = policy
            db.execute(
                "UPDATE skill_trace_policy SET policy=? WHERE id=1", (encoded(policy.model_dump()),)
            )
        self.maintain(db)
        if self.count(db) >= self.policy.trace.max_hot_records:
            raise SkillPolicyError("skill_trace_capacity")

    def read(self, scope, run):
        """Scope comes from a trusted owner binding, like the other private stores."""
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT archive_id,payload,sha256,policy_sha256 FROM skill_trace_archives WHERE scope=? AND run_id=?",
                (scope, run),
            ).fetchall()
        results = []
        for identity, body, checksum, policy in rows:
            payload = json.loads(body)
            if sha(payload) != checksum or any(
                row["scope"] != scope or row["run_id"] != run
                for records in payload.values()
                for row in records
            ):
                raise SkillPolicyError("skill_archive_integrity")
            results.append(
                {
                    "archive_id": identity,
                    "sha256": checksum,
                    "policy_sha256": policy,
                    "records": payload,
                }
            )
        return results

    def preview_policy(self, candidate):
        """A shorter retention requires this concrete preview digest at application."""
        with closing(sqlite3.connect(self.path)) as db:
            affected = db.execute(
                "SELECT archive_id,sha256 FROM skill_trace_archives WHERE finished_at<? ORDER BY archive_id",
                (self.clock() - candidate.trace.archive_retention_days * 86400,),
            ).fetchall()
        result = {
            "current": self.policy.sha256,
            "candidate": candidate.sha256,
            "affected_archives": affected,
        }
        return {**result, "preview_sha256": sha(result)}

    def apply_policy(self, candidate, preview_sha256):
        if self.preview_policy(candidate)["preview_sha256"] != preview_sha256:
            raise SkillPolicyError("skill_retention_preview_changed")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE skill_trace_policy SET policy=? WHERE id=1",
                (encoded(candidate.model_dump()),),
            )
        self.policy = candidate
