"""Revisioned drafts, trusted trials and human-confirmed transactional releases."""

from __future__ import annotations

import difflib
import json
import time
from contextlib import closing
from uuid import uuid4

from hermes_runtime.release_store import ReleaseStore, canonical
from hermes_runtime.skill_authoring import SECTIONS
from hermes_runtime.skill_contracts import dependency_depth
from hermes_runtime.skill_policy import load_policy
from hermes_runtime.skills import NAME, SkillPolicyError, digest, frontmatter, relative_path


class AuthoringError(ValueError):
    pass


class AuthoringStore:
    def __init__(self, root, state=None, *, clock=time.time):
        self.releases = ReleaseStore(root, state)
        self.root, self.clock = self.releases.root, clock
        self.releases.initialize()
        with closing(self.releases.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, owner TEXT NOT NULL, head INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(draft TEXT NOT NULL REFERENCES drafts(id), revision INTEGER NOT NULL, base TEXT NOT NULL REFERENCES releases(sha), sha TEXT NOT NULL, content TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(draft,revision));
                CREATE TABLE IF NOT EXISTS trials(id TEXT PRIMARY KEY, draft TEXT NOT NULL, revision INTEGER NOT NULL, candidate TEXT NOT NULL REFERENCES releases(sha), source TEXT NOT NULL, state TEXT NOT NULL, report TEXT, at REAL NOT NULL, FOREIGN KEY(draft,revision) REFERENCES revisions(draft,revision));
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_trial ON trials(state) WHERE state='running';
                CREATE TABLE IF NOT EXISTS release_previews(id TEXT PRIMARY KEY, owner TEXT NOT NULL, base TEXT NOT NULL, generation INTEGER NOT NULL, target TEXT NOT NULL, draft TEXT, revision INTEGER, trial TEXT, details TEXT NOT NULL, sha TEXT NOT NULL, expires REAL NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
            """)

    @staticmethod
    def identity(value):
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise AuthoringError("resource_not_found")
        return value

    def _revision(self, db, actor, draft, revision=None):
        self.identity(draft)
        row = db.execute(
            "SELECT head FROM drafts WHERE id=? AND owner=?", (draft, actor)
        ).fetchone()
        if row is None:
            raise AuthoringError("resource_not_found")
        selected = row[0] if revision is None else revision
        found = db.execute(
            "SELECT * FROM revisions WHERE draft=? AND revision=?", (draft, selected)
        ).fetchone()
        if found is None:
            raise AuthoringError("resource_not_found")
        return dict(found)

    def list(self, actor):
        with closing(self.releases.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,head,created FROM drafts WHERE owner=? ORDER BY created DESC LIMIT 100",
                    (actor,),
                )
            ]

    def get(self, actor, draft, revision=None):
        with closing(self.releases.connect()) as db:
            row = self._revision(db, actor, draft, revision)
        row["content"] = json.loads(row["content"])
        return row

    def history(self, actor, draft):
        with closing(self.releases.connect()) as db:
            self._revision(db, actor, draft)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT revision,base,sha,at FROM revisions WHERE draft=? ORDER BY revision",
                    (draft,),
                )
            ]

    def write(self, actor, content, *, draft=None, expected=None, base=None):
        with closing(self.releases.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            draft, revision = self._write_revision(
                db, actor, content, draft=draft, expected=expected, base=base
            )
        return self.get(actor, draft, revision)

    def _write_revision(self, db, actor, content, *, draft=None, expected=None, base=None):
        """Write inside the caller's transaction, including learning provenance links."""
        encoded = canonical(content)
        if len(encoded.encode()) > 524288:
            raise AuthoringError("draft_too_large")
        # Shape validation does not require a valid executable draft; incomplete work can be saved.
        if (
            not isinstance(content, dict)
            or set(content) != {"changes"}
            or not isinstance(content["changes"], list)
            or not 1 <= len(content["changes"]) <= 16
        ):
            raise AuthoringError("invalid_draft_shape")
        if draft is None:
            if (
                db.execute("SELECT count(*) FROM drafts WHERE owner=?", (actor,)).fetchone()[0]
                >= 100
            ):
                raise AuthoringError("draft_capacity")
            draft, revision = uuid4().hex, 1
            base = base or db.execute("SELECT sha FROM active WHERE id=1").fetchone()[0]
            self.releases.load(base)
            db.execute("INSERT INTO drafts VALUES(?,?,1,?)", (draft, actor, self.clock()))
        else:
            previous = self._revision(db, actor, draft)
            if expected != previous["sha"]:
                raise AuthoringError("draft_revision_conflict")
            revision = previous["revision"] + 1
            if revision > 200:
                raise AuthoringError("revision_capacity")
            if base is not None and base != previous["base"]:
                self.releases.load(base)
            base = base or previous["base"]
            db.execute("UPDATE drafts SET head=? WHERE id=?", (revision, draft))
        sha = digest(canonical({"base": base, "revision": revision, "content": content}).encode())
        db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?)",
            (draft, revision, base, sha, encoded, self.clock()),
        )
        return draft, revision

    def candidate(self, row):
        baseline = self.releases.load(row["base"])
        manifest = json.loads(canonical(baseline.manifest))
        files = dict(baseline.files)
        entries = {entry["name"]: entry for entry in manifest["skills"]}
        allowed = {
            op
            for entry in self.releases.baseline().entries.values()
            for op in entry["allowed_operations"]
        }
        seen = set()
        for change in row["content"]["changes"]:
            if not isinstance(change, dict) or set(change) != {
                "name",
                "version",
                "allowed_operations",
                "requires_skills",
                "contract",
                "files",
            }:
                raise AuthoringError("invalid_draft_metadata")
            name = change["name"]
            if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen:
                raise AuthoringError("invalid_draft_identity")
            seen.add(name)
            if not isinstance(change["version"], str) or not 1 <= len(change["version"]) <= 64:
                raise AuthoringError("invalid_draft_version")
            if name in entries and change["version"] == entries[name]["version"]:
                raise AuthoringError("changed_skill_requires_new_version")
            operations = change["allowed_operations"]
            if (
                not isinstance(operations, list)
                or not all(isinstance(op, str) for op in operations)
                or set(operations) - allowed
            ):
                raise AuthoringError("draft_uses_unregistered_operations")
            resources = change["files"]
            if (
                not isinstance(resources, dict)
                or "SKILL.md" not in resources
                or not 1 <= len(resources) <= 64
            ):
                raise AuthoringError("invalid_draft_resources")
            hashes = {}
            files = {p: data for p, data in files.items() if not p.startswith(name + "/")}
            for path, body in resources.items():
                relative_path(path)
                if path != "SKILL.md" and path.split("/")[0] not in {
                    "references",
                    "templates",
                    "assets",
                    "scripts",
                }:
                    raise AuthoringError("draft_script_requires_s7b")
                if not isinstance(body, str) or len(body.encode()) > 65536 or "\x00" in body:
                    raise AuthoringError("invalid_draft_resource")
                files[name + "/" + path] = body.encode()
                hashes[path] = digest(body.encode())
            body = resources["SKILL.md"]
            frontmatter(body, name)
            if not all("## " + section in body for section in SECTIONS):
                raise AuthoringError("draft_template_sections_missing")
            entries[name] = {k: v for k, v in change.items() if k != "files"} | {"files": hashes}
        manifest["skills"] = list(entries.values())
        manifest["bundle_version"] = "author-" + row["sha"][:24]
        release = self.releases.install(manifest, files)
        if dependency_depth(release.entries) > load_policy(self.root).composition.max_depth:
            raise AuthoringError("skill_composition_depth")
        return release

    def diff(self, actor, draft, revision=None):
        row = self.get(actor, draft, revision)
        candidate = self.candidate(row)
        return self.release_diff(row["base"], candidate.bundle_sha256)

    def release_diff(self, base, target):
        previous, candidate = self.releases.load(base), self.releases.load(target)
        changes = []
        for name in sorted(previous.files.keys() | candidate.files.keys()):
            before, after = previous.files.get(name, b""), candidate.files.get(name, b"")
            if before != after:
                changes.append(
                    {
                        "path": name,
                        "before": digest(before) if before else None,
                        "after": digest(after) if after else None,
                        "diff": "".join(
                            difflib.unified_diff(
                                before.decode().splitlines(True),
                                after.decode().splitlines(True),
                                fromfile="before/" + name,
                                tofile="after/" + name,
                            )
                        ),
                    }
                )
        metadata = {
            name: {"before": previous.entries.get(name), "after": candidate.entries.get(name)}
            for name in previous.entries.keys() | candidate.entries.keys()
            if previous.entries.get(name) != candidate.entries.get(name)
        }
        return {"base": base, "target": target, "files": changes, "metadata": metadata}

    def source_sha(self):
        # Bind trial/preview to all execution code, fixed holdout suite and deployment policy.
        from application.skill_jobs import SOURCES

        paths = [
            *SOURCES,
            "src/hermes_runtime/authoring_store.py",
            "src/hermes_runtime/skill_learning.py",
            "src/hermes_runtime/experience_learning.py",
            "src/hermes_runtime/autonomy_config.py",
            "src/hermes_runtime/daily_budget.py",
            "src/hermes_runtime/honcho_memory.py",
            "src/hermes_runtime/author_bridge.py",
            "src/hermes_runtime/script_language.py",
            "src/hermes_runtime/script_resources.py",
            "src/hermes_runtime/script_policy.py",
            "src/hermes_runtime/script_runtime.py",
            "src/hermes_runtime/script_bridge.py",
            "scripts/run_skill_script_child.py",
            "src/hermes_runtime/script_trials.py",
            "config/skill-scripts-policy.json",
            "scripts/run_auto_skill_learning.py",
            "src/hermes_runtime/author_trials.py",
            "src/service/skill_authoring_api.py",
            "scripts/evaluate_skills_s6.py",
            "src/hermes_runtime/stability.py",
            "scripts/run_skill_author_trial.py",
            "evaluations/hermes_s6_holdout_v1.json",
            "config/skills-policy.yaml",
        ]
        return digest(canonical({p: digest((self.root / p).read_bytes()) for p in paths}).encode())

    def begin_trial(self, actor, draft, revision=None):
        row = self.get(actor, draft, revision)
        release = self.candidate(row)
        trial = uuid4().hex
        with closing(self.releases.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self._revision(db, actor, draft)["sha"] != row["sha"]:
                raise AuthoringError("draft_revision_conflict")
            try:
                db.execute(
                    "INSERT INTO trials VALUES(?,?,?,?,?,'running',NULL,?)",
                    (
                        trial,
                        draft,
                        row["revision"],
                        release.bundle_sha256,
                        self.source_sha(),
                        self.clock(),
                    ),
                )
            except __import__("sqlite3").IntegrityError:
                raise AuthoringError("trial_already_running") from None
        return self.trial(actor, trial)

    def trial(self, actor, trial):
        self.identity(trial)
        with closing(self.releases.connect()) as db:
            found = db.execute(
                "SELECT t.* FROM trials t JOIN drafts d ON t.draft=d.id WHERE t.id=? AND d.owner=?",
                (trial, actor),
            ).fetchone()
        if found is None:
            raise AuthoringError("resource_not_found")
        row = dict(found)
        row["report"] = json.loads(row["report"]) if row["report"] else None
        return row

    def finish_trial(self, trial, report):
        # Internal worker API only: never expose arbitrary report submission over HTTP.
        with closing(self.releases.connect()) as db, db:
            row = db.execute(
                "SELECT * FROM trials WHERE id=? AND state='running'", (trial,)
            ).fetchone()
            if row is None:
                raise AuthoringError("trial_not_running")
            passed = (
                report.get("status") == "passed"
                and report.get("bundle_sha256") == row["candidate"]
                and report.get("synthetic_only") is True
                and report.get("temporary_home_removed") is True
                and report.get("owned_processes_remaining") == 0
            )
            from hermes_runtime.script_resources import descriptors

            candidate = self.releases.load(row["candidate"])
            scripts = descriptors(candidate.entries, candidate.files)
            if scripts:
                expected = {
                    (name, path, digest(candidate.files[name + "/" + path]))
                    for name, path in scripts
                }
                evidence = report.get("script_cases") or {}
                actual = {
                    (item.get("skill"), item.get("path"), item.get("sha256"))
                    for item in evidence.get("scripts", [])
                }
                native = report.get("script_native_runs", {})
                passed = passed and (
                    evidence.get("status") == "passed"
                    and evidence.get("bundle_sha256") == row["candidate"]
                    and evidence.get("synthetic_only") is True
                    and actual == expected
                    and bool(evidence.get("cases"))
                    and all(item.get("passed") is True for item in evidence["cases"])
                    and all(native.get(name + "/" + path, 0) >= 3 for name, path in scripts)
                )
            db.execute(
                "UPDATE trials SET state=?,report=? WHERE id=?",
                ("passed" if passed else "failed", canonical(report), trial),
            )

    def recover_trials(self):
        # Called under the exclusive author-service process lock, after previous worker group cleanup.
        with closing(self.releases.connect()) as db, db:
            return db.execute(
                "UPDATE trials SET state='interrupted',report=? WHERE state='running'",
                (canonical({"status": "interrupted", "retry_required": True}),),
            ).rowcount

    def preview(self, actor, *, draft=None, trial=None, rollback=None):
        with closing(self.releases.connect()) as db:
            active = db.execute("SELECT sha,generation FROM active WHERE id=1").fetchone()
        revision = None
        if rollback:
            with closing(self.releases.connect()) as db:
                if not db.execute(
                    "SELECT 1 FROM release_events WHERE previous=? OR target=?",
                    (rollback, rollback),
                ).fetchone():
                    raise AuthoringError("rollback_target_not_in_history")
            target = self.releases.load(rollback).bundle_sha256
        else:
            row = self.get(actor, draft)
            evidence = self.trial(actor, trial)
            if (
                row["base"] != active[0]
                or evidence["draft"] != draft
                or evidence["revision"] != row["revision"]
                or evidence["state"] != "passed"
                or evidence["source"] != self.source_sha()
            ):
                raise AuthoringError("fresh_successful_trial_required")
            target, revision = evidence["candidate"], row["revision"]
            if self.candidate(row).bundle_sha256 != target:
                raise AuthoringError("trial_candidate_changed")
        details = self.release_diff(active[0], target) | {
            "generation": active[1],
            "draft": draft,
            "revision": revision,
            "trial": trial,
            "source": self.source_sha(),
            "rollback": bool(rollback),
        }
        identity, sha = uuid4().hex, digest(canonical(details).encode())
        expires = self.clock() + 600
        with closing(self.releases.connect()) as db, db:
            db.execute(
                "INSERT INTO release_previews VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    identity,
                    actor,
                    active[0],
                    active[1],
                    target,
                    draft,
                    revision,
                    trial,
                    canonical(details),
                    sha,
                    expires,
                ),
            )
        return {
            "id": identity,
            "sha256": sha,
            "expires": expires,
            "confirmation_required": True,
            "details": details,
        }

    def confirm(self, actor, preview, sha, *, confirmed):
        if confirmed is not True:
            raise AuthoringError("human_confirmation_required")
        self.identity(preview)
        with closing(self.releases.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM release_previews WHERE id=? AND owner=?", (preview, actor)
            ).fetchone()
            if row is None:
                raise AuthoringError("resource_not_found")
            if row["sha"] != sha:
                raise AuthoringError("preview_digest_mismatch")
            if row["applied"]:
                return {"applied": True, "target": row["target"], "replayed": True}
            active = db.execute("SELECT sha,generation FROM active WHERE id=1").fetchone()
            details = json.loads(row["details"])
            if (
                row["expires"] <= self.clock()
                or tuple(active) != (row["base"], row["generation"])
                or details["source"] != self.source_sha()
            ):
                raise AuthoringError("release_preview_stale")
            if row["draft"]:
                revision = self._revision(db, actor, row["draft"])
                if revision["revision"] != row["revision"]:
                    raise AuthoringError("release_preview_stale")
                evidence = db.execute(
                    "SELECT state,source FROM trials WHERE id=?", (row["trial"],)
                ).fetchone()
                if evidence is None or evidence[0] != "passed" or evidence[1] != details["source"]:
                    raise AuthoringError("fresh_successful_trial_required")
            # Re-open every byte immediately before the transactional pointer change.
            self.releases.load(row["target"])
            if self.release_diff(row["base"], row["target"]) != {
                k: details[k] for k in ("base", "target", "files", "metadata")
            }:
                raise AuthoringError("release_content_changed")
            db.execute(
                "UPDATE active SET sha=?,generation=generation+1 WHERE id=1", (row["target"],)
            )
            db.execute(
                "INSERT INTO release_events(at,actor,previous,target,preview) VALUES(?,?,?,?,?)",
                (self.clock(), actor, row["base"], row["target"], preview),
            )
            db.execute("UPDATE release_previews SET applied=1 WHERE id=?", (preview,))
        return {"applied": True, "target": row["target"], "replayed": False}

    def validate(self, actor, draft):
        try:
            row = self.get(actor, draft)
            release = self.candidate(row)
            return {
                "valid": True,
                "revision": row["revision"],
                "sha256": row["sha"],
                "candidate": release.bundle_sha256,
                "published": False,
            }
        except (
            AuthoringError,
            SkillPolicyError,
            TypeError,
            KeyError,
            UnicodeError,
            OSError,
        ) as exc:
            return {
                "valid": False,
                "error": str(exc) if isinstance(exc, ValueError) else "draft_validation_failed",
                "published": False,
            }
