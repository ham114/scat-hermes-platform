import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_runtime.entrypoint import resolve_profile
from hermes_runtime.skill_cache import SkillCatalogCache
from hermes_runtime.skill_evaluation import offline_bridge
from hermes_runtime.skill_guard import SkillOperationGuard
from hermes_runtime.skill_policy import CachePolicy, SkillPolicy
from hermes_runtime.skills import SkillPolicyError, SkillRelease
from hermes_runtime.trace_lifecycle import TraceLifecycle

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bridge(tmp_path):
    # A real checked-in release with a project-owned profile, not a copied skill mock.
    import shutil
    from uuid import uuid4

    home = ROOT / ".tmp/s6" / ("lifecycle-" + uuid4().hex)
    home.mkdir(parents=True)
    b = offline_bridge(
        SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json"),
        home,
        SkillPolicy(trace={"max_hot_records": 3}),
    )
    yield b
    assert home.resolve().is_relative_to(ROOT / ".tmp/s6")
    shutil.rmtree(home)


def load(bridge, session="s"):
    return json.loads(
        bridge.handler("skill_view")({"name": "scat-flight-query"}, session_id=session)
    )


def test_auto_archive_and_scoped_read_survive_restart(bridge):
    for i in range(8):
        bridge.bind("s", "owner", str(i))
        assert load(bridge)["success"]
        bridge.finish_session("s")
    life = TraceLifecycle(bridge.trace_path, bridge.policy)
    cold = life.read("owner", "0")
    assert cold[0]["records"]["skill_loads"][0]["skill_id"] == "scat-flight-query"
    assert life.read("other", "0") == []
    with sqlite3.connect(bridge.trace_path) as db:
        assert life.count(db) < 3
    with pytest.raises(SkillPolicyError, match="requires_new_run"):
        bridge.bind("new", "owner", "0")


def test_active_and_legacy_unknown_evidence_is_not_deleted(bridge):
    bridge.bind("s", "owner", "run")
    for _ in range(3):
        assert load(bridge)["success"]
    assert load(bridge)["error"] == "skill_trace_capacity"
    with sqlite3.connect(bridge.trace_path) as db, db:
        db.execute("DELETE FROM skill_trace_runs")
    assert load(bridge)["error"] == "skill_trace_capacity"


def test_unfinished_dispatch_and_hold_protect_complete_run(bridge):
    guard = SkillOperationGuard(bridge, {"search"}, set())
    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    with sqlite3.connect(bridge.trace_path) as db, db:
        db.execute(
            "INSERT INTO skill_operations(started_at,scope,run_id,session_id,operation,arguments_sha256,skill_id,skill_version,bundle_sha256,file_sha256,load_id,outcome) VALUES(0,'owner','run','s','search','a','x','1','a','b',1,'admitted')"
        )
    bridge.finish_session("s")
    with sqlite3.connect(bridge.trace_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 0
        db.execute("UPDATE skill_operations SET outcome='returned',finished_at=1")
    bridge.lifecycle.hold("owner", "run")
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 0
    bridge.lifecycle.hold("owner", "run", False)
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 2
    cold = bridge.lifecycle.read("owner", "run")[0]["records"]
    assert cold["skill_operations"][0]["load_id"] == cold["skill_loads"][0]["id"]
    assert guard.bridge is bridge


@pytest.mark.parametrize("failure", ["archive_write", "hot_delete"])
def test_interrupted_archive_or_full_disk_rolls_back_both_sides(bridge, failure):
    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")
    table, action = (
        ("skill_trace_archives", "INSERT")
        if failure == "archive_write"
        else ("skill_loads", "DELETE")
    )
    with sqlite3.connect(bridge.trace_path) as db, db:
        db.execute(
            f"CREATE TRIGGER fail BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'disk full'); END"
        )
    with pytest.raises(sqlite3.Error), sqlite3.connect(bridge.trace_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        bridge.lifecycle.maintain(db, force=True)
    with sqlite3.connect(bridge.trace_path) as db:
        assert bridge.lifecycle.count(db) == 1
        assert db.execute("SELECT count(*) FROM skill_trace_archives").fetchone()[0] == 0


def test_archive_integrity_and_retention_preview_are_required(bridge):
    clock = [100.0]
    bridge.lifecycle.clock = lambda: clock[0]
    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")
    with sqlite3.connect(bridge.trace_path) as db, db:
        bridge.lifecycle.maintain(db, force=True)
    shorter = SkillPolicy(
        trace={"max_hot_records": 3, "hot_retention_days": 1, "archive_retention_days": 2}
    )
    with pytest.raises(SkillPolicyError, match="preview_required"):
        TraceLifecycle(bridge.trace_path, shorter)
    with pytest.raises(SkillPolicyError, match="preview_changed"):
        bridge.lifecycle.apply_policy(shorter, "bad")
    preview = bridge.lifecycle.preview_policy(shorter)
    bridge.lifecycle.apply_policy(shorter, preview["preview_sha256"])
    assert TraceLifecycle(bridge.trace_path, shorter).policy == shorter
    with sqlite3.connect(bridge.trace_path) as db, db:
        db.execute("UPDATE skill_trace_archives SET payload='{}'")
    with pytest.raises(SkillPolicyError, match="integrity"):
        bridge.lifecycle.read("owner", "run")


def test_retention_uses_completion_time_and_preserves_holds(bridge):
    now = [100.0]
    bridge.lifecycle.clock = lambda: now[0]
    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")
    with sqlite3.connect(bridge.trace_path) as db, db:
        bridge.lifecycle.maintain(db, force=True)
    bridge.lifecycle.hold("owner", "run")
    now[0] += 181 * 86400
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db)["purged_archives"] == 0
    bridge.lifecycle.hold("owner", "run", False)
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db)["purged_archives"] == 1
    assert not bridge.lifecycle.read("owner", "run")


@pytest.mark.parametrize("state", ["pending", "running", "unknown", "cancel_requested"])
def test_referencing_job_blocks_archive_until_all_jobs_end(bridge, state):
    from application.task_store import digest
    from hermes_runtime.trace_references import track_parent

    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")
    path = bridge.trace_path.parent / "jobs.sqlite"
    body = {"skills": {"parent_budget": {"run_id": "run"}}}
    with sqlite3.connect(path) as db, db:
        db.execute(
            "CREATE TABLE model_tasks(task_id TEXT,scope TEXT,state TEXT,expires REAL,snapshot TEXT,sha256 TEXT)"
        )
        db.execute("CREATE TABLE skill_jobs(task_id TEXT,state TEXT)")
        for identity in ("a", "b"):
            db.execute(
                "INSERT INTO model_tasks VALUES(?, 'owner', 'queued', ?, ?, ?)",
                (identity, 99999999999, json.dumps(body), digest(body)),
            )
            db.execute("INSERT INTO skill_jobs VALUES(?,?)", (identity, state))
    track_parent(
        ROOT,
        "owner",
        {"path": bridge.trace_path.relative_to(ROOT).as_posix(), "run_id": "run"},
        path,
    )
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 0
    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE skill_jobs SET state='succeeded' WHERE task_id='a'")
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 0
    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE skill_jobs SET state='cancelled'")
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 1
    # Even a cold receipt is protected from total-retention purge if a job is unresolved.
    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE skill_jobs SET state='unknown' WHERE task_id='b'")
    bridge.lifecycle.clock = lambda: 999999999999
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db)["purged_archives"] == 0
    path.unlink()
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.maintain(db)["purged_archives"] == 0


def test_concurrent_archive_has_single_copy(bridge):
    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")

    def move(_):
        with sqlite3.connect(bridge.trace_path, timeout=5) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return bridge.lifecycle.maintain(db, force=True)["archived_records"]

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(move, range(4))) == 1
    assert len(bridge.lifecycle.read("owner", "run")) == 1


def test_real_sqlite_full_does_not_lose_hot_receipt(bridge):
    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")
    with sqlite3.connect(bridge.trace_path) as db, db:
        # A private SQLite page limit exercises SQLITE_FULL without filling the server disk.
        db.execute("UPDATE skill_loads SET skill_id=?", ("x" * 100000,))
        db.commit()
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={pages}")
        with pytest.raises(sqlite3.OperationalError, match="full"):
            db.execute("BEGIN IMMEDIATE")
            bridge.lifecycle.maintain(db, force=True)
    with sqlite3.connect(bridge.trace_path) as db:
        assert bridge.lifecycle.count(db) == 1
        assert db.execute("SELECT count(*) FROM skill_trace_archives").fetchone()[0] == 0


@pytest.mark.skipif(
    __import__("sys").platform != "linux", reason="real process kill acceptance uses server Linux"
)
def test_real_process_death_before_archive_commit_preserves_receipt(bridge):
    import os
    import signal
    import subprocess
    import sys

    bridge.bind("s", "owner", "run")
    assert load(bridge)["success"]
    bridge.finish_session("s")
    script = """import sys,sqlite3,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from hermes_runtime.trace_lifecycle import TraceLifecycle
from hermes_runtime.skill_policy import SkillPolicy
db=sqlite3.connect(sys.argv[2]); db.execute('BEGIN IMMEDIATE')
life=object.__new__(TraceLifecycle);life.path=Path(sys.argv[2]);life.policy=SkillPolicy();life.clock=time.time;life.root=Path(sys.argv[1]).parent
life.maintain(db,force=True)
print('before_commit',flush=True)
time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(ROOT / "src"), str(bridge.trace_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "before_commit"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
    with sqlite3.connect(bridge.trace_path) as db, db:
        assert bridge.lifecycle.count(db) == 1
        assert bridge.lifecycle.maintain(db, force=True)["archived_records"] == 1
    assert len(bridge.lifecycle.read("owner", "run")) == 1


def test_cache_ttl_lru_scope_release_and_copy_isolation():
    now = [0]
    cache = SkillCatalogCache(CachePolicy(max_entries=2, ttl_seconds=10), clock=lambda: now[0])
    calls = []

    def loader():
        calls.append(1)
        return {"items": [7]}

    key = ("owner", "release", "permissions")
    cache.get(key, loader)["items"].append(8)
    assert cache.get(key, loader) == {"items": [7]} and len(calls) == 1
    cache.get(("other", "release", "permissions"), loader)
    cache.get(("owner", "new-release", "permissions"), loader)
    assert len(cache.entries) == 2
    cache.get(key, loader)
    assert len(calls) == 4
    now[0] = 11
    cache.get(key, loader)
    assert len(calls) == 5
    cache.invalidate()
    assert not cache.entries


def test_cache_hit_cannot_skip_filesystem_check_or_authorize_an_operation(bridge):
    bridge.bind("s", "owner", "run")
    bridge.handler("skills_list")({}, session_id="s")
    bridge.handler("skills_list")({}, session_id="s")
    assert bridge.catalog_cache.hits == 1
    guard = SkillOperationGuard(bridge, {"mcp__scat__search_scat_flights"}, set())
    result = json.loads(
        guard.invoke(
            lambda *a, **k: pytest.fail("not admitted"),
            "mcp__scat__search_scat_flights",
            {},
            session_id="s",
        )
    )
    assert result["error"] == "skill_required"
    (bridge.folder / "unexpected.md").write_text("unpublished")
    assert not json.loads(bridge.handler("skills_list")({}, session_id="s"))["success"]


def test_idle_session_release_protects_active_session(bridge, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("hermes_runtime.skills.time.time", lambda: clock[0])
    bridge.policy = bridge.policy.model_copy(
        update={
            "sessions": bridge.policy.sessions.model_copy(
                update={"max_active": 1, "idle_ttl_seconds": 1}
            )
        }
    )
    bridge.bind("s", "owner", "one")
    clock[0] += 10
    with pytest.raises(SkillPolicyError, match="session_capacity"):
        bridge.bind("other", "owner", "two")
    bridge.finish_session("s")
    clock[0] += 2
    bridge.bind("other", "owner", "two")
    assert "s" not in bridge._sessions


def test_new_entrypoint_defaults_and_explicit_history_profile():
    args = SimpleNamespace(
        legacy_h5=False,
        background_job=None,
        with_mcp=False,
        with_skills=False,
        with_model_tasks=False,
        with_workflows=False,
    )
    assert resolve_profile(args).with_workflows and args.with_skills
    old = SimpleNamespace(
        legacy_h5=True, background_job=None, with_skills=False, with_workflows=False
    )
    assert resolve_profile(old) is old
    old.with_skills = True
    with pytest.raises(ValueError, match="cannot_enable"):
        resolve_profile(old)
