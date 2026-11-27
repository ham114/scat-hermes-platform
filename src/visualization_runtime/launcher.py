"""Trusted render service: resolves all paths and data from fenced database records."""

import hashlib
import json
import threading
from uuid import uuid4

from application.context import ExecutionContext
from visualization_runtime.ledger import ExecutionLedger
from visualization_runtime.paths import confined_path
from visualization_runtime.sandbox import RenderError, file_bytes, validate_png
from visualization_runtime.snapshots import digest
from visualization_runtime.store import JobConflict, canonical


class RenderLauncher:
    def __init__(self, jobs, snapshots, sandbox):
        self.jobs, self.snapshots, self.sandbox = jobs, snapshots, sandbox
        self.ledger = ExecutionLedger(jobs)
        self._locks = {}
        self._lock_guard = threading.Lock()

    def _lock(self, task_id):
        with self._lock_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def read(self, render_id):
        with self.jobs.transaction() as db:
            row = db.execute(
                "SELECT * FROM visualization_renders WHERE render_id=?", (render_id,)
            ).fetchone()
        if row is None or digest(json.loads(row["manifest"])) != row["manifest_sha"]:
            raise JobConflict("render_receipt_integrity_failed")
        manifest = json.loads(row["manifest"])
        for item in manifest.get("images", []):
            raw = self.image_bytes(render_id, item["viewport"])
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise JobConflict("render_image_changed")
        return manifest

    def image_bytes(self, render_id, viewport):
        import re

        from visualization_runtime.sandbox import SIZES

        if not re.fullmatch(r"[0-9a-f]{32}", render_id) or viewport not in SIZES:
            raise RenderError("invalid_render_reference")
        path = confined_path(
            self.snapshots.workspace_root,
            self.snapshots.root / "renders" / render_id / "output" / (viewport + ".png"),
        )
        raw = file_bytes(path)
        validate_png(raw, viewport)
        return raw

    def render(self, task_id, worker, generation, attempt_id):
        with self._lock(task_id):
            return self._render(task_id, worker, generation, attempt_id)

    def cleanup_task(self, task_id, generation):
        with self._lock(task_id):
            with self.jobs.transaction() as db:
                names = [
                    r[0]
                    for r in db.execute(
                        "SELECT resource_id FROM visualization_resources WHERE task_id=? AND generation=? AND state='active'",
                        (task_id, generation),
                    )
                ]
            for name in names:
                self.sandbox.remove(name)

    def _render(self, task_id, worker, generation, attempt_id):
        with self.jobs.transaction() as db:
            job = self.jobs._fence(db, task_id, worker, generation)
            if job["phase"] != "rendering":
                raise JobConflict("render_phase_required")
            request = self.jobs._decode(job)["request"]
            attempt = db.execute(
                "SELECT * FROM visualization_attempts WHERE attempt_id=? AND task_id=?",
                (attempt_id, task_id),
            ).fetchone()
            if (
                attempt is None
                or hashlib.sha256(attempt["source"].encode()).hexdigest() != attempt["source_sha"]
            ):
                raise JobConflict("visualization_source_integrity_failed")
            old = db.execute(
                "SELECT * FROM visualization_renders WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if old:
                old_manifest = json.loads(old["manifest"])
                if old_manifest.get("state") != "rendering":
                    return self.read_after_transaction(old["render_id"])
                if old_manifest["generation"] >= generation:
                    raise JobConflict("render_already_in_progress")
                if db.execute(
                    "SELECT 1 FROM visualization_resources WHERE task_id=? AND state='active'",
                    (task_id,),
                ).fetchone():
                    raise JobConflict("render_cleanup_required")
            render_id = uuid4().hex
            binding = {
                "render_id": render_id,
                "attempt_id": attempt_id,
                "source_sha256": attempt["source_sha"],
                "snapshot_sha256": request.snapshot_sha256,
                "preferences_sha256": request.preferences_sha256,
                "generation": generation,
                "state": "rendering",
                "images": [],
            }
            if old:
                db.execute(
                    "DELETE FROM visualization_renders WHERE render_id=?", (old["render_id"],)
                )
            db.execute(
                "INSERT INTO visualization_renders VALUES(?,?,?,?,?,?)",
                (
                    render_id,
                    task_id,
                    attempt_id,
                    canonical(binding),
                    digest(binding),
                    self.jobs.clock(),
                ),
            )
        context = ExecutionContext(job["owner"], job["thread"])
        snapshot = self.snapshots.load(context, request.snapshot_id, request.snapshot_sha256)
        name = "vis-" + render_id + "-g" + str(generation)
        self.ledger.register_resource(
            task_id, worker, generation, attempt_id=attempt_id, resource_id=name
        )
        try:
            result = self.sandbox.render(
                name,
                self.snapshots.root / "renders" / render_id,
                {
                    "source": attempt["source"],
                    "snapshot": snapshot["payload"],
                    "viewports": list(request.viewports),
                },
            )
        except RenderError as error:
            result = {"state": "failed", "error": str(error), "images": []}
        finally:
            self.ledger.confirm_resource_stopped(
                task_id, worker, generation, name, verify_stopped=self.sandbox.stopped
            )
        manifest = binding | result
        with self.jobs.transaction() as db:
            self.jobs._fence(db, task_id, worker, generation)
            db.execute(
                "UPDATE visualization_renders SET manifest=?,manifest_sha=? WHERE render_id=?",
                (canonical(manifest), digest(manifest), render_id),
            )
        return manifest

    def read_after_transaction(self, render_id):
        # This branch only returns an immutable manifest; image revalidation is
        # performed by the worker/reviewer outside the SQLite transaction.
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.jobs.path)) as db:
            row = db.execute(
                "SELECT manifest,manifest_sha FROM visualization_renders WHERE render_id=?",
                (render_id,),
            ).fetchone()
        if row is None or digest(json.loads(row[0])) != row[1]:
            raise JobConflict("render_receipt_integrity_failed")
        return json.loads(row[0])
