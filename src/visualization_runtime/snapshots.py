"""Materialize authorized receipts without inventing completeness or querying new data."""

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from visualization_runtime.contracts import SourceReference
from visualization_runtime.paths import confined_path
from visualization_runtime.store import JobConflict, JobNotFound, canonical


class SnapshotUnavailable(LookupError):
    pass


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def array_counts(value, path="", depth=0):
    if depth > 24:
        raise ValueError("snapshot_nesting_limit")
    counts = {}
    if isinstance(value, dict):
        for key, item in value.items():
            counts.update(array_counts(item, path + "/" + key, depth + 1))
    elif isinstance(value, list):
        counts[path or "/"] = len(value)
        # Nested structures are retained verbatim; cap the number of records
        # conservatively across arrays, rather than labeling them a population.
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                counts.update(array_counts(item, path + "/" + str(index), depth + 1))
    return counts


class SnapshotStore:
    def __init__(
        self,
        jobs,
        root: Path,
        *,
        workspace_root: Path,
        max_bytes=20 * 1024 * 1024,
        max_rows=100000,
        ttl=7 * 86400,
    ):
        self.jobs = jobs
        self.workspace_root = workspace_root.resolve()
        self.root = confined_path(self.workspace_root, root)
        if max_bytes < 1 or max_rows < 1 or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("invalid_snapshot_limits")
        self.max_bytes, self.max_rows, self.ttl = max_bytes, max_rows, ttl

    def _path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise SnapshotUnavailable("snapshot_not_found")
        return confined_path(self.workspace_root, self.root / "snapshots" / (identity + ".json"))

    def load(self, context, identity, expected_sha=None):
        with self.jobs.transaction() as db:
            row = db.execute(
                "SELECT * FROM visualization_snapshots WHERE snapshot_id=? AND owner=? "
                "AND thread=? AND revoked=0 AND expires>?",
                (identity, context.user_id, context.thread_id, self.jobs.clock()),
            ).fetchone()
        if row is None:
            raise SnapshotUnavailable("snapshot_not_found")
        if expected_sha is not None and expected_sha != row["payload_sha"]:
            raise JobConflict("snapshot_reference_changed")
        path = self._path(identity)
        try:
            with path.open("rb") as stream:
                raw = stream.read(min(row["bytes"], self.max_bytes) + 1)
        except OSError:
            raise SnapshotUnavailable("snapshot_file_unavailable") from None
        if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["payload_sha"]:
            raise JobConflict("snapshot_integrity_failed")
        return {
            "snapshot_id": identity,
            "payload_sha256": row["payload_sha"],
            "payload": json.loads(raw),
            "expires_at": row["expires"],
        }

    async def capture(self, context, request_id, references, *, receipts, artifacts=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("invalid_snapshot_request_id")
        refs = tuple(SourceReference.model_validate(ref) for ref in references)
        if not 1 <= len(refs) <= 16 or len({(r.kind, r.source_id) for r in refs}) != len(refs):
            raise ValueError("invalid_snapshot_sources")
        refs_sha = digest([ref.model_dump(mode="json") for ref in refs])
        with self.jobs.transaction() as db:
            old = db.execute(
                "SELECT * FROM visualization_snapshots WHERE owner=? AND thread=? AND request_id=?",
                (context.user_id, context.thread_id, request_id),
            ).fetchone()
        if old:
            if old["references_sha"] != refs_sha:
                raise JobConflict("snapshot_request_id_conflict")
            return self.load(context, old["snapshot_id"])
        datasets = []
        row_budget = 0
        for ref in refs:
            if ref.kind == "readonly_result":
                receipt = await asyncio.to_thread(
                    receipts.get, context.scope_key, ref.run_id, ref.source_id
                )
                payload = receipt["result"]
                if (
                    receipt["payload_sha256"] != ref.payload_sha256
                    or digest(payload) != ref.payload_sha256
                ):
                    raise JobConflict("snapshot_source_changed")
                if payload.get("status") != "ok" or payload.get("representation") == "page_summary":
                    raise SnapshotUnavailable("snapshot_requires_actual_data")
                operation = receipt["operation"]
            else:
                if artifacts is None:
                    raise SnapshotUnavailable("artifact_repository_unavailable")
                record = await artifacts.get_for_owner(ref.source_id, owner_user_id=context.user_id)
                if (
                    record is None
                    or record.owner_user_id != context.user_id
                    or record.thread_id != context.thread_id
                ):
                    raise SnapshotUnavailable("snapshot_source_not_found")
                if record.expires_at is not None and record.expires_at <= datetime.fromtimestamp(
                    self.jobs.clock(), UTC
                ):
                    raise SnapshotUnavailable("snapshot_source_expired")
                payload = record.analysis_result.model_dump(mode="json")
                if (
                    record.run_id != ref.run_id
                    or record.payload_sha256 != ref.payload_sha256
                    or digest(payload) != ref.payload_sha256
                ):
                    raise JobConflict("snapshot_source_changed")
                operation = record.analysis_result.operation
            counts = array_counts(payload)
            row_budget += sum(counts.values())
            if row_budget > self.max_rows:
                raise ValueError("snapshot_row_limit")
            datasets.append(
                {
                    "reference": ref.model_dump(mode="json"),
                    "operation": operation,
                    "scope": "fetched_payload",
                    "completeness": "not_asserted",
                    "array_lengths": counts,
                    "payload": payload,
                }
            )
            # Bound accumulation before canonicalizing the combined body.
            if len(canonical(datasets).encode()) > self.max_bytes:
                raise ValueError("snapshot_byte_limit")
        raw = canonical({"schema_version": 1, "datasets": datasets}).encode()
        if len(raw) > self.max_bytes:
            raise ValueError("snapshot_byte_limit")
        identity, now = uuid4().hex, self.jobs.clock()
        path = self._path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        path.chmod(0o600)
        try:
            with self.jobs.transaction() as db:
                for ref in refs:
                    if db.execute(
                        "SELECT 1 FROM visualization_revoked_sources WHERE owner=? AND kind=? AND source_id=?",
                        (context.user_id, ref.kind, ref.source_id),
                    ).fetchone():
                        raise SnapshotUnavailable("snapshot_source_revoked")
                old = db.execute(
                    "SELECT * FROM visualization_snapshots WHERE owner=? AND thread=? "
                    "AND request_id=?",
                    (context.user_id, context.thread_id, request_id),
                ).fetchone()
                if old:
                    if old["references_sha"] != refs_sha:
                        raise JobConflict("snapshot_request_id_conflict")
                    identity = old["snapshot_id"]
                else:
                    db.execute(
                        "INSERT INTO visualization_snapshots VALUES(?,?,?,?,?,?,?,?,?,0)",
                        (
                            identity,
                            context.user_id,
                            context.thread_id,
                            request_id,
                            refs_sha,
                            hashlib.sha256(raw).hexdigest(),
                            len(raw),
                            now,
                            now + self.ttl,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO visualization_snapshot_sources VALUES(?,?,?)",
                        [(identity, ref.kind, ref.source_id) for ref in refs],
                    )
            if old:
                path.unlink()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return self.load(context, identity)

    def revoke(self, context, identity):
        with self.jobs.transaction() as db:
            changed = db.execute(
                "UPDATE visualization_snapshots SET revoked=1 WHERE snapshot_id=? "
                "AND owner=? AND thread=?",
                (identity, context.user_id, context.thread_id),
            ).rowcount
            if not changed:
                raise JobNotFound("snapshot_not_found")

    def revoke_source(self, context, kind, source_id):
        with self.jobs.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO visualization_revoked_sources VALUES(?,?,?)",
                (context.user_id, kind, source_id),
            )
            return db.execute(
                "UPDATE visualization_snapshots SET revoked=1 WHERE owner=? AND snapshot_id IN "
                "(SELECT snapshot_id FROM visualization_snapshot_sources WHERE kind=? AND source_id=?)",
                (context.user_id, kind, source_id),
            ).rowcount
