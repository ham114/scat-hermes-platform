"""Content-addressed skill releases and transactionally pinned run identities.

No author credential, application imports, dependency installation or script execution.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from hermes_runtime.skills import SkillPolicyError, SkillRelease, checked_path, digest

SHA = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class ReleaseStore:
    def __init__(self, root, state=None):
        self.root = Path(root).resolve()
        value = state or os.environ.get("SCAT_SKILL_RELEASE_STATE", "var/skills/catalog.sqlite")
        path = Path(value)
        self.path = checked_path(self.root, path.as_posix())
        self.bundles = self.path.parent / "bundles"

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def baseline(self):
        return SkillRelease(self.root, self.root / "services/hermes/skills.lock.json")

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS releases(sha TEXT PRIMARY KEY, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS active(id INTEGER PRIMARY KEY CHECK(id=1), sha TEXT NOT NULL REFERENCES releases(sha), generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS run_releases(scope TEXT NOT NULL, run TEXT NOT NULL, sha TEXT NOT NULL REFERENCES releases(sha), PRIMARY KEY(scope,run));
                CREATE TABLE IF NOT EXISTS release_events(id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, previous TEXT NOT NULL, target TEXT NOT NULL, preview TEXT UNIQUE NOT NULL);
            """)
        self.path.chmod(0o600)
        baseline = self.baseline()
        self.install(baseline.manifest, baseline.files)
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO active VALUES(1,?,0)", (baseline.bundle_sha256,))

    def install(self, manifest, files):
        sha = digest(canonical(manifest).encode())
        target = checked_path(self.root, (self.bundles / sha).relative_to(self.root).as_posix())
        if target.exists():
            release = self.load(sha, require_registered=False)
        else:
            # Stage beside the destination: project tmp and release storage can
            # be separate Docker mounts, which cannot be atomically renamed.
            temporary = checked_path(
                self.root,
                (self.bundles / (".install-" + uuid4().hex)).relative_to(self.root).as_posix(),
            )
            temporary.mkdir(parents=True)
            try:
                for rel, payload in files.items():
                    path = checked_path(temporary / "skills", rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    path.chmod(0o444)
                lock = temporary / "lock.json"
                lock.write_text(canonical(manifest), encoding="utf-8")
                with lock.open("rb") as stream:
                    os.fsync(stream.fileno())
                lock.chmod(0o444)
                SkillRelease(self.root, lock, skills_folder=temporary / "skills")
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    temporary.rename(target)
                except FileExistsError:
                    pass
                # Flush parent directory metadata before any active DB pointer can commit.
                if os.name == "posix":
                    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                release = self.load(sha, require_registered=False)
            finally:
                if temporary.exists():
                    for path in temporary.rglob("*"):
                        if path.is_file():
                            path.chmod(0o600)
                    shutil.rmtree(temporary)
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO releases VALUES(?,unixepoch())", (sha,))
        return release

    def load(self, sha, *, require_registered=True):
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise SkillPolicyError("invalid_release_identity")
        if require_registered:
            with closing(self.connect()) as db:
                if not db.execute("SELECT 1 FROM releases WHERE sha=?", (sha,)).fetchone():
                    raise SkillPolicyError("release_not_registered")
        folder = checked_path(self.root, (self.bundles / sha).relative_to(self.root).as_posix())
        release = SkillRelease(self.root, folder / "lock.json", skills_folder=folder / "skills")
        if release.bundle_sha256 != sha:
            raise SkillPolicyError("release_identity_mismatch")
        return release

    def current(self):
        if not self.path.exists():
            return self.baseline()
        with closing(self.connect()) as db:
            row = db.execute("SELECT sha FROM active WHERE id=1").fetchone()
        if row is None:
            raise SkillPolicyError("active_release_missing")
        return self.load(row[0])

    def resolve(self, sha=None):
        if sha is None:
            return self.current()
        if not self.path.exists():
            release = self.baseline()
            if sha == release.bundle_sha256:
                return release
            raise SkillPolicyError("release_not_registered")
        return self.load(sha)

    def pin(self, scope, run, *, sha=None):
        if not all(isinstance(x, str) and x for x in (scope, run)):
            raise SkillPolicyError("trusted_release_context_required")
        if not self.path.exists():
            return self.resolve(sha)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT sha FROM run_releases WHERE scope=? AND run=?", (scope, run)
            ).fetchone()
            if existing:
                if sha and existing[0] != sha:
                    raise SkillPolicyError("run_release_changed")
                selected = existing[0]
            else:
                selected = sha or db.execute("SELECT sha FROM active WHERE id=1").fetchone()[0]
                self.load(selected)
                db.execute("INSERT INTO run_releases VALUES(?,?,?)", (scope, run, selected))
        return self.load(selected)


def resolve_release(root, sha=None):
    return ReleaseStore(root).resolve(sha)


def planning_release(root, scope, run):
    store = ReleaseStore(root)
    return store.pin(scope, run) if run else store.current()
