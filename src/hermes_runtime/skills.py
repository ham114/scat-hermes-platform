"""Admitted native Hermes skills, immutable per-profile snapshots and trusted load receipts.

This module never executes skill scripts or installs dependencies. Writing/publishing skills
is deliberately a separate future capability from the two runtime read tools.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock

try:
    from yaml import YAMLError
    from yaml import safe_load as yaml_load
except ImportError:
    # Pinned Hermes uses ruamel.yaml; the platform environment already has PyYAML.
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    def yaml_load(content):
        return YAML(typ="safe", pure=True).load(content)


SKILL_TOOLS = frozenset({"skills_list", "skill_view"})
PINNED_COMMIT = "5b147ab036d6d19131bff8d20701b5001eab844a"
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class SkillPolicyError(ValueError):
    """A fixed, non-sensitive policy code; never includes native error content."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SkillPolicyError("invalid_skill_path")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.drive
        or ":" in value
        or any(x in {"", ".", ".."} for x in value.split("/"))
    ):
        raise SkillPolicyError("invalid_skill_path")
    return value


def checked_path(root: Path, value: str) -> Path:
    path = root / relative_path(value)
    for component in [path, *path.parents]:
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise SkillPolicyError("skill_link_rejected")
        if component == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise SkillPolicyError("invalid_skill_path")
    return path


def frontmatter(content: str, name: str) -> dict:
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise SkillPolicyError("skill_frontmatter_required")
    try:
        end = lines.index("---", 1)
        header = "\n".join(lines[1:end])
        if len(header) > 3000:
            raise SkillPolicyError("skill_frontmatter_too_large")
        data = yaml_load(header)
    except (ValueError, YAMLError):
        raise SkillPolicyError("invalid_skill_frontmatter") from None
    if not isinstance(data, dict):
        raise SkillPolicyError("invalid_skill_frontmatter")
    # Native skill_view can call pm.ensure even with preprocess=False.
    if data.get("deps"):
        raise SkillPolicyError("skill_auto_dependencies_rejected")
    if set(data) - {"name", "description", "deps"}:
        raise SkillPolicyError("skill_frontmatter_field_rejected")
    if data.get("name") != name or not isinstance(data.get("description"), str):
        raise SkillPolicyError("skill_identity_mismatch")
    if not 1 <= len(data["description"].strip()) <= 800:
        raise SkillPolicyError("invalid_skill_description")
    return data


class SkillRelease:
    """Load an explicit checked-in release lock, never generate approval from runtime files."""

    def __init__(self, root: Path, manifest: Path, *, skills_folder: Path | None = None):
        self.root = root.resolve()
        self.skills_folder = skills_folder or self.root / "skills"
        checked_path(self.root, self.skills_folder.relative_to(self.root).as_posix())
        manifest = checked_path(self.root, manifest.relative_to(self.root).as_posix())
        if manifest.stat().st_size > 65536:
            raise SkillPolicyError("skill_manifest_too_large")
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            self.manifest.get("schema_version") not in {1, 2}
            or self.manifest.get("upstream_commit") != PINNED_COMMIT
            or not isinstance(self.manifest.get("bundle_version"), str)
        ):
            raise SkillPolicyError("skill_release_protocol_mismatch")
        self.bundle_sha256 = digest(
            json.dumps(
                self.manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
        entries = self.manifest.get("skills")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
            raise SkillPolicyError("invalid_skill_release")
        self.entries, self.files = {}, {}
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not NAME.fullmatch(name) or name in self.entries:
                raise SkillPolicyError("invalid_skill_identity")
            if not isinstance(entry.get("version"), str) or not entry["version"]:
                raise SkillPolicyError("invalid_skill_version")
            operations = entry.get("allowed_operations")
            if not isinstance(operations, list) or not all(isinstance(x, str) for x in operations):
                raise SkillPolicyError("invalid_skill_operations")
            files = entry.get("files")
            if not isinstance(files, dict) or "SKILL.md" not in files or len(files) > 64:
                raise SkillPolicyError("invalid_skill_files")
            for rel, expected in files.items():
                rel = relative_path(rel)
                if rel != "SKILL.md" and rel.split("/")[0] not in {
                    "references",
                    "assets",
                    "templates",
                    "scripts",
                }:
                    raise SkillPolicyError("skill_support_path_rejected")
                file = checked_path(self.skills_folder, f"{name}/{rel}")
                if file.stat().st_size > 65536:
                    raise SkillPolicyError("skill_file_too_large")
                data = file.read_bytes()
                if digest(data) != expected:
                    raise SkillPolicyError("skill_digest_mismatch")
                data.decode("utf-8")
                self.files[f"{name}/{rel}"] = data
            entry = dict(entry)
            entry["description"] = frontmatter(
                self.files[f"{name}/SKILL.md"].decode("utf-8"), name
            )["description"]
            self.entries[name] = entry
        if sum(map(len, self.files.values())) > 524288:
            raise SkillPolicyError("skill_bundle_too_large")
        if self.manifest["schema_version"] == 2:
            from hermes_runtime.skill_contracts import validate_composition

            try:
                validate_composition(list(self.entries.values()))
            except ValueError as exc:
                raise SkillPolicyError(str(exc)) from None
            from hermes_runtime.script_resources import descriptors

            try:
                descriptors(self.entries, self.files)
            except ValueError as exc:
                raise SkillPolicyError(str(exc)) from None
        self.verify_tree(self.skills_folder)

    def verify_tree(self, folder: Path) -> None:
        actual = set()
        for path in folder.rglob("*"):
            rel = path.relative_to(folder).as_posix()
            checked_path(folder, rel)
            if path.is_file():
                actual.add(rel)
                if (
                    path.stat().st_size > 65536
                    or rel not in self.files
                    or digest(path.read_bytes()) != digest(self.files[rel])
                ):
                    raise SkillPolicyError("skill_snapshot_changed")
        if actual != set(self.files):
            raise SkillPolicyError("skill_snapshot_changed")

    def stage(self, home: Path) -> Path:
        home = checked_path(self.root, home.relative_to(self.root).as_posix())
        folder = home / "skills"
        if not folder.exists():
            folder.mkdir(parents=True)
        # Hermes may create an empty skills skeleton while importing its gateway.
        # Only an entirely empty directory can be initialized; partial releases fail closed.
        if not any(folder.iterdir()):
            for rel, data in self.files.items():
                target = checked_path(folder, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
                target.chmod(0o444)
        self.verify_tree(folder)
        return folder

    def verify_native_source(self, source: Path) -> None:
        expected = self.manifest.get("native_sources", {})
        if (
            not expected
            or json.loads((self.root / "services/hermes/upstream.lock.json").read_text())["commit"]
            != PINNED_COMMIT
        ):
            raise SkillPolicyError("native_skill_source_mismatch")
        for rel, sha in expected.items():
            if digest(checked_path(source, rel).read_bytes()) != sha:
                raise SkillPolicyError("native_skill_source_mismatch")


class NativeSkillBridge:
    def __init__(self, release: SkillRelease, folder: Path, native, trace_path: Path, policy=None):
        self.release, self.folder, self.native = release, folder, native
        from hermes_runtime.skill_policy import SkillPolicy

        self.policy = policy or SkillPolicy()
        from hermes_runtime.skill_contracts import dependency_depth

        if dependency_depth(release.entries) > self.policy.composition.max_depth:
            raise SkillPolicyError("skill_composition_depth")
        self._sessions = {}
        self._session_state = {}
        self._budgets = {}
        self._lock = RLock()
        self.trace_path = checked_path(
            release.root, trace_path.relative_to(release.root).as_posix()
        )
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.trace_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS skill_loads (
                id INTEGER PRIMARY KEY, loaded_at REAL NOT NULL,
                scope TEXT NOT NULL, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
                skill_id TEXT NOT NULL, version TEXT NOT NULL, bundle_sha256 TEXT NOT NULL,
                file_path TEXT NOT NULL, file_sha256 TEXT NOT NULL)""")
        self.trace_path.chmod(0o600)
        from hermes_runtime.skill_cache import SkillCatalogCache
        from hermes_runtime.trace_lifecycle import TraceLifecycle

        self.lifecycle = TraceLifecycle(self.trace_path, self.policy, workspace_root=release.root)
        self.catalog_cache = SkillCatalogCache(self.policy.cache)
        self._verify()

    def _verify(self):
        self.release.verify_tree(self.folder)
        roots, active = self.native.skill_search_dirs()
        if active.resolve() != self.folder.resolve() or {path.resolve() for _, path in roots} != {
            self.folder.resolve()
        }:
            raise SkillPolicyError("native_skill_roots_rejected")

    def bind(self, session_id, scope, run_id):
        if not all(isinstance(x, str) and x for x in (session_id, scope, run_id)):
            raise SkillPolicyError("trusted_skill_context_required")
        with self._lock:
            now = time.time()
            for sid, (active, touched) in list(self._session_state.items()):
                if not active and now - touched >= self.policy.sessions.idle_ttl_seconds:
                    self._sessions.pop(sid, None)
                    self._session_state.pop(sid, None)
                    self._budgets.pop(sid, None)
            if session_id in self._sessions and self._sessions[session_id][0] != scope:
                raise SkillPolicyError("skill_session_scope_changed")
            if (
                session_id not in self._sessions
                and len(self._sessions) >= self.policy.sessions.max_active
            ):
                raise SkillPolicyError("skill_session_capacity")
            self.lifecycle.bind(scope, run_id)
            self._sessions[session_id] = (scope, run_id)
            self._session_state[session_id] = (True, now)

    def finish_session(self, session_id):
        with self._lock:
            if session_id in self._sessions:
                self.lifecycle.finish(*self._sessions[session_id])
                self._session_state[session_id] = (False, time.time())

    def trace_count(self, db):
        count = db.execute("SELECT count(*) FROM skill_loads").fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_operations'").fetchone():
            count += db.execute("SELECT count(*) FROM skill_operations").fetchone()[0]
        return count

    def schemas(self):
        return [self.native.SKILLS_LIST_SCHEMA, self.native.SKILL_VIEW_SCHEMA]

    def session_bridge(self, session_id):
        return self

    def handler(self, tool):
        if tool not in SKILL_TOOLS:
            raise SkillPolicyError("skill_tool_rejected")

        def invoke(args, *, session_id="", **kwargs):
            try:
                with self._lock:
                    trusted = self._sessions.get(session_id)
                    if trusted is None:
                        raise SkillPolicyError("trusted_skill_context_required")
                    self._verify()
                    if not isinstance(args, dict):
                        raise SkillPolicyError("invalid_skill_arguments")
                    if tool == "skills_list":
                        if set(args) - {"category"} or (
                            "category" in args and not isinstance(args["category"], str)
                        ):
                            raise SkillPolicyError("invalid_skill_arguments")
                        # _verify above always runs, including cache hits. Never cache
                        # current-run load receipts or business results.
                        result = self.catalog_cache.get(
                            (
                                trusted[0],
                                self.release.bundle_sha256,
                                tuple(sorted(self.release.entries)),
                            ),
                            lambda: json.loads(self.native.skills_list(task_id=session_id)),
                        )
                        if not result.get("success") or {
                            item["name"] for item in result.get("skills", [])
                        } != set(self.release.entries):
                            raise SkillPolicyError("native_skill_catalog_mismatch")
                        if args.get("category"):
                            result["skills"] = [
                                x for x in result["skills"] if x.get("category") == args["category"]
                            ]
                        result["count"] = len(result["skills"])
                        if self.release.manifest["schema_version"] == 2:
                            for item in result["skills"]:
                                entry = self.release.entries[item["name"]]
                                item.update(
                                    version=entry["version"],
                                    contract=entry["contract"],
                                    requires_skills=entry["requires_skills"],
                                    allowed_operations=entry["allowed_operations"],
                                )
                    else:
                        if set(args) - {"name", "file_path"}:
                            raise SkillPolicyError("invalid_skill_arguments")
                        name = args.get("name")
                        if not isinstance(name, str) or name not in self.release.entries:
                            raise SkillPolicyError("skill_not_published")
                        rel = relative_path(args.get("file_path", "SKILL.md"))
                        key = f"{name}/{rel}"
                        if key not in self.release.files:
                            raise SkillPolicyError("skill_file_not_published")
                        result = json.loads(
                            self.native.skill_view(
                                name,
                                file_path=None if rel == "SKILL.md" else rel,
                                task_id=session_id,
                                preprocess=False,
                            )
                        )
                        expected = self.release.files[key].decode("utf-8")
                        if not result.get("success") or result.get("content") != expected:
                            raise SkillPolicyError("native_skill_content_mismatch")
                        self._verify()
                        entry = self.release.entries[name]
                        with closing(sqlite3.connect(self.trace_path)) as db, db:
                            db.execute("BEGIN IMMEDIATE")
                            self.lifecycle.reserve(db, policy=self.policy)
                            db.execute(
                                "INSERT INTO skill_loads VALUES(NULL,?,?,?,?,?,?,?,?,?)",
                                (
                                    time.time(),
                                    *trusted,
                                    session_id,
                                    name,
                                    entry["version"],
                                    self.release.bundle_sha256,
                                    rel,
                                    digest(self.release.files[key]),
                                ),
                            )
                        result = {
                            "success": True,
                            "name": name,
                            "file": rel,
                            "content": expected,
                            "version": entry["version"],
                            "file_sha256": digest(self.release.files[key]),
                        }
                    result["bundle_sha256"] = self.release.bundle_sha256
                    return json.dumps(result, ensure_ascii=False)
            except SkillPolicyError as exc:
                return json.dumps({"success": False, "error": str(exc)})
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                return json.dumps({"success": False, "error": "skill_read_unavailable"})

        return invoke


def register_native_skills(
    release, home, source, trace_path, policy=None, *, store=None, pinned_sha=None
):
    """Only call in a dedicated pinned Hermes process after setting HERMES_HOME."""
    release.verify_native_source(source)
    folder = release.stage(home if store is None else home / "releases" / release.bundle_sha256)
    from tools import skills_tool as native
    from tools.registry import registry

    if Path(native.__file__).resolve() != (source / "tools/skills_tool.py").resolve():
        raise SkillPolicyError("native_skill_source_mismatch")
    if store is None:
        bridge = NativeSkillBridge(release, folder, native, trace_path, policy=policy)
    else:
        from hermes_runtime.release_bridge import ReleaseRoutingBridge

        bridge = ReleaseRoutingBridge(release, home, native, trace_path, store, policy, pinned_sha)
    for schema in bridge.schemas():
        registry.register(
            name=schema["name"],
            toolset="scat_skills",
            schema=schema,
            handler=bridge.handler(schema["name"]),
            override=True,
            execution_timeout=15,
        )
    if set(registry.get_tool_names_for_toolset("scat_skills")) != SKILL_TOOLS:
        raise SkillPolicyError("native_skill_registry_mismatch")
    return bridge
