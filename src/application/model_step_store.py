"""Versioned, scoped S3 step receipts. No pickle, arbitrary paths or automatic retries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class StepError(ValueError):
    """Only fixed error codes cross the tool boundary."""


class StepReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = Field(
        default="model-step-reference/v1", pattern="^model-step-reference/v1$"
    )
    artifact_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: str = Field(pattern=r"^(input|prediction|evaluation|result)$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelStepStore:
    def __init__(self, path: Path, *, workspace_root: Path, fence=None):
        root, path = workspace_root.resolve(), path.absolute()
        if not path.resolve().is_relative_to(root):
            raise StepError("step_store_outside_project")
        if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
            raise StepError("step_store_link_rejected")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fence = fence
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_step_artifacts (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, run TEXT NOT NULL,
                    kind TEXT NOT NULL, sha TEXT NOT NULL, body TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS model_step_receipts (
                    scope TEXT NOT NULL, run TEXT NOT NULL, step_key TEXT NOT NULL,
                    operation TEXT NOT NULL, state TEXT NOT NULL, reference TEXT, error TEXT,
                    PRIMARY KEY(scope, run, step_key));
            """)
        path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def reserve(self, scope, run, operation, arguments):
        key = digest({"operation": operation, "arguments": arguments})
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.fence:
                self.fence(db)
            row = db.execute(
                "SELECT * FROM model_step_receipts WHERE scope=? AND run=? AND step_key=?",
                (scope, run, key),
            ).fetchone()
            if row:
                if row["state"] == "succeeded":
                    return key, StepReference.model_validate_json(row["reference"])
                if row["state"] == "prepared":
                    db.execute(
                        "UPDATE model_step_receipts SET state='running',error=NULL WHERE scope=? AND run=? AND step_key=? AND state='prepared'",
                        (scope, run, key),
                    )
                    return key, None
                raise StepError(
                    "step_" + ("unknown" if row["state"] == "running" else row["state"])
                )
            db.execute(
                "INSERT INTO model_step_receipts VALUES (?,?,?,?, 'running',NULL,NULL)",
                (scope, run, key, operation),
            )
        return key, None

    def fail(self, scope, run, key, code, *, unknown=False):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.fence:
                self.fence(db)
            db.execute(
                "UPDATE model_step_receipts SET state=?,error=? WHERE scope=? AND run=? AND step_key=? AND state='running'",
                ("unknown" if unknown else "failed", code, scope, run, key),
            )

    def save(self, scope, run, key, kind, body, *, expires):
        raw = canonical(body)
        if len(raw.encode()) > 16 * 1024 * 1024:
            raise StepError("step_artifact_too_large")
        ref = StepReference(artifact_id=uuid4().hex, kind=kind, content_sha256=digest(body))
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.fence:
                self.fence(db)
            row = db.execute(
                "SELECT state FROM model_step_receipts WHERE scope=? AND run=? AND step_key=?",
                (scope, run, key),
            ).fetchone()
            if row is None or row[0] != "running":
                raise StepError("step_receipt_not_running")
            db.execute(
                "INSERT INTO model_step_artifacts VALUES (?,?,?,?,?,?,?)",
                (ref.artifact_id, scope, run, kind, ref.content_sha256, raw, expires),
            )
            db.execute(
                "UPDATE model_step_receipts SET state='succeeded',reference=? WHERE scope=? AND run=? AND step_key=?",
                (ref.model_dump_json(), scope, run, key),
            )
        return ref

    def read(self, scope, run, reference, *, kind):
        ref = StepReference.model_validate(reference)
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM model_step_artifacts WHERE id=? AND scope=? AND run=?",
                (ref.artifact_id, scope, run),
            ).fetchone()
        if row is None:
            raise StepError("step_artifact_not_found")
        if row["expires"] <= time.time():
            raise StepError("step_artifact_expired")
        if ref.kind != kind or row["kind"] != kind:
            raise StepError("step_artifact_kind_mismatch")
        body = json.loads(row["body"])
        if row["sha"] != ref.content_sha256 or digest(body) != ref.content_sha256:
            raise StepError("step_artifact_integrity_failed")
        return body
