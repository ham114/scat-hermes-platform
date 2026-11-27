"""Only verified, immutable render/review evidence can complete a visualization."""

import hashlib
import json
from uuid import uuid4

from visualization_runtime.ledger import ExecutionLedger
from visualization_runtime.paths import confined_path
from visualization_runtime.review import parse_review
from visualization_runtime.sandbox import file_bytes
from visualization_runtime.snapshots import digest
from visualization_runtime.store import JobConflict, JobNotFound, canonical


class Publisher:
    def __init__(self, jobs, snapshots, launcher):
        self.jobs, self.snapshots, self.launcher = jobs, snapshots, launcher

    def _snapshot_access(self, db, context, snapshot_id, *, require_unexpired=False):
        row = db.execute(
            "SELECT * FROM visualization_snapshots WHERE snapshot_id=? AND owner=? AND thread=? AND revoked=0",
            (snapshot_id, context.user_id, context.thread_id),
        ).fetchone()
        if row is None or require_unexpired and row["expires"] <= self.jobs.clock():
            raise JobNotFound("visualization_source_unavailable")

    def get(self, context, artifact_id):
        with self.jobs.transaction() as db:
            row = db.execute(
                "SELECT * FROM visualization_publications WHERE artifact_id=? AND owner=? AND thread=?",
                (artifact_id, context.user_id, context.thread_id),
            ).fetchone()
            if row is None:
                raise JobNotFound("visualization_artifact_not_found")
            manifest = json.loads(row["manifest"])
            if digest(manifest) != row["manifest_sha"]:
                raise JobConflict("publication_integrity_failed")
            self._snapshot_access(db, context, manifest["snapshot_id"])
        return manifest

    def read_file(self, context, artifact_id, file_id):
        manifest = self.get(context, artifact_id)
        if file_id not in manifest["files"]:
            raise JobNotFound("visualization_file_not_found")
        path = confined_path(
            self.snapshots.workspace_root,
            self.snapshots.root / "published" / manifest["artifact_id"] / file_id,
        )
        raw = file_bytes(path)
        if hashlib.sha256(raw).hexdigest() != manifest["files"][file_id]:
            raise JobConflict("publication_file_changed")
        return raw

    def publish(self, context, task_id, worker, generation, render_id):
        job = self.jobs.get(context, task_id)
        with self.jobs.transaction() as db:
            old = db.execute(
                "SELECT artifact_id FROM visualization_publications WHERE task_id=?", (task_id,)
            ).fetchone()
        if old:
            return self.get(context, old["artifact_id"])
        request = job["request"]
        snapshot = self.snapshots.load(context, request.snapshot_id, request.snapshot_sha256)
        render = self.launcher.read(render_id)
        with self.jobs.transaction() as db:
            current = self.jobs._fence(db, task_id, worker, generation)
            if current["phase"] != "publishing":
                raise JobConflict("publication_phase_required")
            ExecutionLedger(self.jobs)._budget(db, current)
            attempt = db.execute(
                "SELECT * FROM visualization_attempts WHERE task_id=? AND attempt_id=?",
                (task_id, render.get("attempt_id")),
            ).fetchone()
            row = db.execute(
                "SELECT * FROM visualization_reviews WHERE render_id=?", (render_id,)
            ).fetchone()
            if (
                attempt is None
                or row is None
                or digest(json.loads(row["report"])) != row["report_sha"]
            ):
                raise JobConflict("verified_review_required")
            review = json.loads(row["report"])
            review_call = db.execute(
                "SELECT * FROM visualization_provider_calls WHERE call_id=? AND task_id=? AND role='review'",
                (row["call_id"], task_id),
            ).fetchone()
            if review_call is None or review_call["fingerprint"] != review["request_sha256"]:
                raise JobConflict("review_call_binding_changed")
            saved = ExecutionLedger.saved_completion(review_call)
            if not saved.model_matches:
                raise JobConflict("review_model_mismatch")
            actual = parse_review(saved.content)
            if actual.model_dump(mode="json") != {
                key: review[key] for key in ("decision", "issues", "checked_viewports")
            }:
                raise JobConflict("review_content_changed")
            if (
                render["state"] != "rendered"
                or review["decision"] != "pass"
                or review["render_sha256"] != digest(render)
                or set(review["checked_viewports"]) != set(request.viewports)
                or {i["viewport"] for i in render["images"]} != set(request.viewports)
                or render["snapshot_sha256"] != snapshot["payload_sha256"]
                or render["preferences_sha256"] != request.preferences_sha256
                or render.get("runtime_image") != self.launcher.sandbox.image
                or render["source_sha256"] != hashlib.sha256(attempt["source"].encode()).hexdigest()
            ):
                raise JobConflict("publication_evidence_changed")
            if db.execute(
                "SELECT 1 FROM visualization_resources WHERE task_id=? AND state='active'",
                (task_id,),
            ).fetchone():
                raise JobConflict("publication_resources_active")
        identity = uuid4().hex
        folder = confined_path(
            self.snapshots.workspace_root, self.snapshots.root / "published" / identity
        )
        folder.mkdir(parents=True, exist_ok=False)
        blobs = {
            i["viewport"] + ".png": self.launcher.image_bytes(render_id, i["viewport"])
            for i in render["images"]
        }
        blobs["source.js"] = attempt["source"].encode()
        for name, raw in blobs.items():
            if name.endswith(".png") and hashlib.sha256(raw).hexdigest() != next(
                i["sha256"] for i in render["images"] if i["file"] == name
            ):
                raise JobConflict("render_image_changed")
            with (folder / name).open("xb") as stream:
                stream.write(raw)
            (folder / name).chmod(0o400)
        manifest = {
            "schema_version": 1,
            "artifact_id": identity,
            "task_id": task_id,
            "version": (request.parent_version or 0) + 1,
            "parent_artifact_id": request.parent_artifact_id,
            "created_at": self.jobs.clock(),
            "data_refs": [item["reference"] for item in snapshot["payload"]["datasets"]],
            "snapshot_id": request.snapshot_id,
            "snapshot_sha256": request.snapshot_sha256,
            "preferences_sha256": request.preferences_sha256,
            "source_sha256": render["source_sha256"],
            "render_id": render_id,
            "render_sha256": digest(render),
            "review_id": review["review_id"],
            "review_sha256": digest(review),
            "runtime_image": render["runtime_image"],
            "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in blobs.items()},
        }
        with self.jobs.transaction() as db:
            current = self.jobs._fence(db, task_id, worker, generation)
            ExecutionLedger(self.jobs)._budget(db, current)
            self._snapshot_access(db, context, request.snapshot_id, require_unexpired=True)
            # The frozen request and copied evidence cannot change during this
            # transaction. Cancellation/revocation wins before successful commit.
            if current["phase"] != "publishing" or current["request_sha"] != job["request_sha"]:
                raise JobConflict("publication_job_changed")
            if db.execute(
                "SELECT 1 FROM visualization_resources WHERE task_id=? AND state='active'",
                (task_id,),
            ).fetchone():
                raise JobConflict("publication_resources_active")
            db.execute(
                "INSERT INTO visualization_publications VALUES(?,?,?,?,?,?,?)",
                (
                    identity,
                    task_id,
                    context.user_id,
                    context.thread_id,
                    canonical(manifest),
                    digest(manifest),
                    self.jobs.clock(),
                ),
            )
            db.execute(
                "UPDATE visualization_jobs SET state='succeeded',lease_until=NULL,updated=? WHERE task_id=?",
                (self.jobs.clock(), task_id),
            )
            self.jobs._event(db, current, "published")
        return manifest
