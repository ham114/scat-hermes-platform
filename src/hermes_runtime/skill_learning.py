"""Bounded experience snapshots for authors; no memory fetch, LLM call or publication."""

from __future__ import annotations

import json
from contextlib import closing
from uuid import uuid4

from hermes_runtime.authoring_store import AuthoringError
from hermes_runtime.release_store import canonical
from hermes_runtime.skills import digest


class SkillLearningStore:
    def __init__(self, authoring):
        self.authoring = authoring
        with closing(authoring.releases.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_learning_candidates(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, sha TEXT NOT NULL,
                    base TEXT NOT NULL REFERENCES releases(sha), snapshot TEXT NOT NULL,
                    created REAL NOT NULL, draft TEXT, revision INTEGER, content_sha TEXT,
                    UNIQUE(owner,sha),
                    FOREIGN KEY(draft,revision) REFERENCES revisions(draft,revision)
                );
            """)

    @staticmethod
    def snapshot(purpose, evidence):
        def text(value, limit):
            return (
                isinstance(value, str)
                and bool(value.strip())
                and len(value.encode()) <= limit
                and "\x00" not in value
            )

        if not text(purpose, 2048) or not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            raise AuthoringError("invalid_learning_snapshot")
        seen = set()
        for item in evidence:
            if (
                not isinstance(item, dict)
                or set(item) != {"kind", "reference", "revision", "summary"}
                or item["kind"] not in ("honcho_memory", "task_review")
                or not text(item["reference"], 256)
                or not text(item["revision"], 128)
                or not text(item["summary"], 4096)
            ):
                raise AuthoringError("invalid_learning_evidence")
            key = (item["kind"], item["reference"], item["revision"])
            if key in seen:
                raise AuthoringError("duplicate_learning_evidence")
            seen.add(key)
        # Normalize ordering only. Preserve supplied wording and revision identifiers exactly.
        ordered = sorted(evidence, key=lambda x: (x["kind"], x["reference"], x["revision"]))
        return {"purpose": purpose, "evidence": ordered}

    def _row(self, db, actor, identity):
        self.authoring.identity(identity)
        row = db.execute(
            "SELECT * FROM skill_learning_candidates WHERE id=? AND owner=?", (identity, actor)
        ).fetchone()
        if row is None:
            raise AuthoringError("resource_not_found")
        return dict(row)

    @staticmethod
    def public(row):
        result = {key: row[key] for key in ("id", "sha", "base", "created", "draft", "revision")}
        result.update(json.loads(row["snapshot"]))
        result.update(
            state="drafted" if row["draft"] else "pending",
            evidence_status="supplied_snapshot",
            publication_triggered=False,
        )
        return result

    def create(self, actor, purpose, evidence):
        snapshot = canonical(self.snapshot(purpose, evidence))
        sha = digest(snapshot.encode())
        with closing(self.authoring.releases.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM skill_learning_candidates WHERE owner=? AND sha=?", (actor, sha)
            ).fetchone()
            if existing:
                return self.public(dict(existing)) | {"replayed": True}
            if (
                db.execute(
                    "SELECT count(*) FROM skill_learning_candidates WHERE owner=?", (actor,)
                ).fetchone()[0]
                >= 100
            ):
                raise AuthoringError("learning_candidate_capacity")
            base = db.execute("SELECT sha FROM active WHERE id=1").fetchone()[0]
            identity = uuid4().hex
            db.execute(
                "INSERT INTO skill_learning_candidates(id,owner,sha,base,snapshot,created) "
                "VALUES(?,?,?,?,?,?)",
                (identity, actor, sha, base, snapshot, self.authoring.clock()),
            )
            row = self._row(db, actor, identity)
        return self.public(row) | {"replayed": False}

    def get(self, actor, identity):
        with closing(self.authoring.releases.connect()) as db:
            return self.public(self._row(db, actor, identity))

    def list(self, actor):
        with closing(self.authoring.releases.connect()) as db:
            # Keep evidence bodies out of list responses; detailed reads are explicit.
            return [
                {key: row[key] for key in ("id", "sha", "base", "created", "draft", "revision")}
                for row in db.execute(
                    "SELECT id,sha,base,created,draft,revision FROM skill_learning_candidates "
                    "WHERE owner=? ORDER BY created DESC,id LIMIT 100",
                    (actor,),
                )
            ]

    def create_draft(self, actor, identity, expected, content):
        content_sha = digest(canonical(content).encode())
        with closing(self.authoring.releases.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, actor, identity)
            if row["sha"] != expected:
                raise AuthoringError("learning_snapshot_conflict")
            replayed = bool(row["draft"])
            if replayed:
                if content_sha != row["content_sha"]:
                    raise AuthoringError("learning_draft_already_created")
            else:
                draft, revision = self.authoring._write_revision(
                    db, actor, content, base=row["base"]
                )
                db.execute(
                    "UPDATE skill_learning_candidates SET draft=?,revision=?,content_sha=? "
                    "WHERE id=? AND owner=?",
                    (draft, revision, content_sha, identity, actor),
                )
                row = self._row(db, actor, identity)
        # Read the originally generated revision, even if an author has since revised it.
        draft = self.authoring.get(actor, row["draft"], row["revision"])
        return {"candidate": self.public(row), "draft": draft, "replayed": replayed}
