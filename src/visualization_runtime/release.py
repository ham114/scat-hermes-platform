"""Read-only release checks and quiesced SQLite/artifact backup. Never enable services."""

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from visualization_runtime.paths import confined_path


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def inspect_database(path):
    with closing(readonly(path)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("release_database_integrity_failed")
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() not in {
            "delete",
            "persist",
            "truncate",
        }:
            raise ValueError("release_rollback_journal_required")
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def verify_artifact_references(root, task_path, artifacts):
    tables = inspect_database(task_path)
    expected = {}
    with closing(readonly(task_path)) as db:
        if "visualization_snapshots" in tables:
            for identity, sha in db.execute(
                "SELECT snapshot_id,payload_sha FROM visualization_snapshots"
            ):
                if not re.fullmatch(r"[0-9a-f]{32}", identity):
                    raise ValueError("release_artifact_reference_invalid")
                expected[artifacts / "snapshots" / (identity + ".json")] = sha
        if "visualization_publications" in tables:
            for identity, raw in db.execute(
                "SELECT artifact_id,manifest FROM visualization_publications"
            ):
                if not re.fullmatch(r"[0-9a-f]{32}", identity):
                    raise ValueError("release_artifact_reference_invalid")
                for name, sha in json.loads(raw)["files"].items():
                    if name not in {"desktop.png", "mobile.png", "source.js"}:
                        raise ValueError("release_artifact_reference_invalid")
                    expected[artifacts / "published" / identity / name] = sha
    for path, sha in expected.items():
        path = confined_path(root, path)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("release_artifact_reference_missing_or_changed")


def audit(root, settings, task_path, parent_path, *, memory_path=None, worker=False):
    checks = {}

    def check(name, operation):
        try:
            operation()
            checks[name] = True
        except (ValueError, OSError, sqlite3.Error, KeyError):
            checks[name] = False  # Never expose connection values, secrets or SQL content.

    def runtime():
        base = root / "services/visualization-renderer"
        lock = json.loads((base / "runtime.lock.json").read_text())
        for file, field in (
            ("render.mjs", "renderer_host_sha256"),
            ("Dockerfile", "dockerfile_sha256"),
            ("package-lock.json", "package_lock_sha256"),
            ("seccomp.json", "seccomp_sha256"),
        ):
            if (
                hashlib.sha256((base / file).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                != lock[field]
            ):
                raise ValueError("release_runtime_changed")
        if settings.sandbox_image != lock["validated_renderer_image_id"]:
            raise ValueError("release_image_unverified")

    def tasks():
        path = confined_path(root, task_path)
        if "visualization_schema" not in inspect_database(path):
            raise ValueError("release_visualization_schema_missing")
        with closing(readonly(path)) as db:
            if [r[0] for r in db.execute("SELECT version FROM visualization_schema")] != [5]:
                raise ValueError("release_visualization_schema_unsupported")

    def parent():
        if parent_path is None or "skill_run_budgets" not in inspect_database(
            confined_path(root, parent_path)
        ):
            raise ValueError("release_parent_budget_missing")

    def memory():
        if memory_path is None or not {
            "memory_users",
            "chart_memory_state",
            "chart_preferences",
        } <= inspect_database(confined_path(root, memory_path)):
            raise ValueError("release_chart_memory_missing")

    def credentials():
        settings.provider("code")
        settings.provider("review")

    check("runtime_lock", runtime)
    check("task_state", tasks)
    check("parent_budget", parent)
    check("chart_memory", memory)
    if worker:
        check("provider_configuration", credentials)
    # These are local prerequisites, not evidence of a deployed gateway/native agent.
    return {
        "local_prerequisites_passed": all(checks.values()),
        "checks": checks,
        "production_flag": settings.enabled,
        "deployed_end_to_end_verified": False,
    }


def backup(root, destination, databases, artifacts):
    """Operator must stop every writer first. No restore or destructive operation here."""
    destination = confined_path(root, destination)
    if destination.exists():
        raise ValueError("release_backup_destination_exists")
    sources = {name: confined_path(root, path) for name, path in databases.items()}
    if "tasks" not in sources or not sources.keys() <= {"tasks", "parent", "memory"}:
        raise ValueError("release_unknown_database")
    artifact_root = confined_path(root, artifacts)
    if destination == artifact_root or destination.is_relative_to(artifact_root):
        raise ValueError("release_backup_inside_artifacts")
    tables = {name: inspect_database(path) for name, path in sources.items()}
    with closing(readonly(sources["tasks"])) as db:
        if (
            "visualization_jobs" in tables["tasks"]
            and db.execute(
                "SELECT 1 FROM visualization_jobs WHERE state IN ('queued','running','needs_reconciliation') LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("release_visualizations_not_drained")
        has_data = (
            "visualization_snapshots" in tables["tasks"]
            and db.execute("SELECT 1 FROM visualization_snapshots LIMIT 1").fetchone() is not None
        )
    if has_data and not artifact_root.is_dir():
        raise ValueError("release_artifacts_missing")
    if artifact_root.exists() and not artifact_root.is_dir():
        raise ValueError("release_artifacts_missing")
    verify_artifact_references(root, sources["tasks"], artifact_root)
    # No symlink traversal or credential directories in the artifact archive.
    paths = list(artifact_root.rglob("*"))
    for path in paths:
        confined_path(root, path)
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("release_backup_artifact_alias")
    destination.mkdir(parents=True)
    destination.chmod(0o700)
    manifest = {"schema_version": 1, "requires_stopped_writers": True, "files": {}}
    for name, path in sources.items():
        if name not in {"tasks", "parent", "memory"}:
            raise ValueError("release_unknown_database")
        target = destination / (name + ".sqlite")
        with closing(readonly(path)) as source, closing(sqlite3.connect(target)) as output:
            source.backup(output)
        target.chmod(0o600)
        manifest["files"][target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    for path in paths:
        if path.is_file():
            relative = Path("artifacts") / path.relative_to(artifact_root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.parent.chmod(0o700)
            shutil.copyfile(path, target)
            target.chmod(0o600)
            manifest["files"][relative.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "manifest.json").chmod(0o600)
    return manifest


def verify_backup(root, destination):
    destination = confined_path(root, destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or not 1 <= len(manifest.get("files", {})) <= 100000:
        raise ValueError("release_backup_manifest_invalid")
    for name, expected in manifest["files"].items():
        path = confined_path(destination, destination / name)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("release_backup_integrity_failed")
        if name in {"tasks.sqlite", "parent.sqlite", "memory.sqlite"}:
            inspect_database(path)
    if "tasks.sqlite" not in manifest["files"]:
        raise ValueError("release_backup_tasks_missing")
    verify_artifact_references(destination, destination / "tasks.sqlite", destination / "artifacts")
    return True
