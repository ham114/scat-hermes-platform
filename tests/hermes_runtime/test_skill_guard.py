import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_runtime.skill_guard import SkillOperationGuard
from hermes_runtime.skills import PINNED_COMMIT, NativeSkillBridge, SkillRelease, digest


@pytest.fixture
def guarded(tmp_path):
    files = {
        "test-query/SKILL.md": "---\nname: test-query\ndescription: query\n---\nQuery",
        "test-query/references/query.md": "Reference only",
        "test-knowledge/SKILL.md": "---\nname: test-knowledge\ndescription: knowledge\n---\nKnowledge",
    }
    entries = []
    for name, operation in [("test-query", "query"), ("test-knowledge", "knowledge")]:
        hashes = {}
        for key, content in files.items():
            if not key.startswith(name + "/"):
                continue
            path = tmp_path / "skills" / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            hashes[key.split("/", 1)[1]] = digest(content.encode())
        entries.append(
            {"name": name, "version": "1", "allowed_operations": [operation], "files": hashes}
        )
    manifest = tmp_path / "lock.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_version": "test",
                "upstream_commit": PINNED_COMMIT,
                "skills": entries,
            }
        )
    )
    release = SkillRelease(tmp_path, manifest)
    folder = release.stage(tmp_path / "home")
    native = SimpleNamespace(
        skill_search_dirs=lambda: ([(1, folder)], folder),
        skill_view=lambda name, file_path, **kw: json.dumps(
            {"success": True, "content": (folder / name / (file_path or "SKILL.md")).read_text()}
        ),
        skills_list=lambda **kw: json.dumps(
            {"success": True, "skills": [{"name": x["name"]} for x in entries]}
        ),
    )
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    bridge.bind("session", "owner:thread", "run-1")
    calls = []

    def dispatch(name, args, **kwargs):
        calls.append((name, args, kwargs))
        return json.dumps({"status": "ok", "synthetic": True})

    registry = SimpleNamespace(dispatch=dispatch)
    guard = SkillOperationGuard(
        bridge, {"query", "knowledge", "scat_task_prepare"}, {"scat_task_get"}
    )
    guard.install(registry)
    return bridge, guard, registry, calls


def load(bridge, name="test-query", session="session", **kwargs):
    result = json.loads(bridge.handler("skill_view")({"name": name, **kwargs}, session_id=session))
    assert result["success"], result


@pytest.mark.parametrize(
    "kind,primary",
    [
        ("forecast", "scat-model-comparison"),
        ("comparison", "scat-model-comparison"),
        ("experiment", "scat-experiment"),
        ("materialization", "scat-data-materialization"),
        ("report", "scat-report"),
    ],
)
def test_every_workflow_kind_recovers_and_requires_its_current_primary_skill(
    tmp_path, kind, primary
):
    root = Path(__file__).resolve().parents[2]
    release = SkillRelease(root, root / "services/hermes/skills.lock.json")
    folder = release.stage(tmp_path / "home")
    native = SimpleNamespace(
        skill_search_dirs=lambda: ([(1, folder)], folder),
        skill_view=lambda name, file_path, **kw: json.dumps(
            {"success": True, "content": (folder / name / (file_path or "SKILL.md")).read_text()}
        ),
    )
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    bridge.bind("session", "owner:thread", "run")
    calls = []
    guard = SkillOperationGuard(bridge, {"scat_workflow_prepare"}, set())

    def dispatch(*args, **kwargs):
        calls.append(True)
        return json.dumps({"status": "ok"})

    args = {"proposal": {"kind": kind}}
    rejected = json.loads(
        guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="session")
    )
    assert rejected["recovery"]["skills"] == [
        {"name": primary, "version": release.entries[primary]["version"], "file_path": "SKILL.md"}
    ]
    load(bridge, "scat-flight-query")
    assert (
        json.loads(guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="session"))[
            "status"
        ]
        == "forbidden"
    )
    load(bridge, primary)
    assert (
        json.loads(guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="session"))[
            "status"
        ]
        == "ok"
    )
    bridge.bind("new-session", "owner:thread", "new-run")
    assert (
        json.loads(guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="new-session"))[
            "status"
        ]
        == "forbidden"
    )
    assert calls == [True]


def invoke(registry, name="query", session="session", args=None, **kwargs):
    return json.loads(
        registry.dispatch(name, {} if args is None else args, session_id=session, **kwargs)
    )


@pytest.mark.parametrize("action", ["none", "list", "reference", "wrong-skill"])
def test_only_an_applicable_body_can_authorize_business(guarded, action):
    bridge, _, registry, calls = guarded
    if action == "list":
        assert json.loads(bridge.handler("skills_list")({}, session_id="session"))["success"]
    elif action == "reference":
        load(bridge, file_path="references/query.md")
    elif action == "wrong-skill":
        load(bridge, "test-knowledge")
    result = invoke(registry)
    assert result["error"] == (
        "skill_operation_not_allowed" if action == "wrong-skill" else "skill_required"
    )
    assert calls == []


def test_matching_load_records_exact_run_skill_and_result(guarded):
    bridge, _, registry, calls = guarded
    load(bridge)
    assert invoke(registry, args={"limit": 1})["status"] == "ok"
    assert len(calls) == 1
    with sqlite3.connect(bridge.trace_path) as db:
        row = db.execute(
            "SELECT scope,run_id,session_id,operation,skill_id,skill_version,bundle_sha256,arguments_sha256,outcome,result_sha256,load_id FROM skill_operations"
        ).fetchone()
        linked = db.execute("SELECT id FROM skill_loads").fetchone()[0]
    assert row[:7] == (
        "owner:thread",
        "run-1",
        "session",
        "query",
        "test-query",
        "1",
        bridge.release.bundle_sha256,
    )
    assert row[7] == digest(b'{"limit":1}')
    assert row[8] == "returned" and len(row[9]) == 64 and row[10] == linked


@pytest.mark.parametrize("session", ["forged", "other-session"])
def test_other_session_cannot_borrow_a_load(guarded, session):
    bridge, _, registry, calls = guarded
    load(bridge)
    if session == "other-session":
        bridge.bind(session, "owner:thread", "run-1")
    result = invoke(registry, session=session, args={"skill_id": "test-query", "run_id": "run-1"})
    assert result["error"] in {"trusted_skill_context_required", "skill_required"}
    assert calls == []


def test_rebind_to_new_run_requires_a_new_load(guarded):
    bridge, _, registry, calls = guarded
    load(bridge)
    bridge.bind("session", "owner:thread", "run-2")
    assert invoke(registry)["error"] == "skill_required"
    assert calls == []
    load(bridge)
    assert invoke(registry)["status"] == "ok"
    with sqlite3.connect(bridge.trace_path) as db:
        assert db.execute("SELECT run_id FROM skill_operations").fetchone()[0] == "run-2"


def test_missing_dependency_recovery_loads_exact_skill_without_bypassing_guard(guarded):
    bridge, _, registry, calls = guarded
    load(bridge, "test-knowledge")
    denied = invoke(registry)
    assert denied["recovery"]["action"] == "skill_view"
    assert denied["recovery"]["skills"] == [
        {"name": "test-query", "version": "1", "file_path": "SKILL.md"}
    ]
    assert calls == []
    # A hint alone never grants authority, including on the next run.
    assert invoke(registry)["status"] == "forbidden"
    load(bridge, denied["recovery"]["skills"][0]["name"])
    assert invoke(registry)["status"] == "ok"
    bridge.bind("session", "owner:thread", "run-next")
    assert invoke(registry)["error"] == "skill_required"
    assert len(calls) == 1


def test_untrusted_session_has_no_catalog_recovery(guarded):
    _, _, registry, calls = guarded
    assert "recovery" not in invoke(registry, session="unknown")
    assert calls == []


def test_cross_scope_cannot_borrow_a_load(guarded):
    bridge, _, registry, calls = guarded
    load(bridge)
    bridge.bind("other", "another-owner:thread", "run-1")
    assert invoke(registry, session="other")["error"] == "skill_required"
    assert calls == []


def test_combination_preserves_per_operation_skill_identity(guarded):
    bridge, _, registry, calls = guarded
    load(bridge)
    load(bridge, "test-knowledge")
    for name in ["query", "knowledge"]:
        assert invoke(registry, name)["status"] == "ok"
    with sqlite3.connect(bridge.trace_path) as db:
        assert db.execute(
            "SELECT operation,skill_id FROM skill_operations ORDER BY id"
        ).fetchall() == [("query", "test-query"), ("knowledge", "test-knowledge")]
    assert len(calls) == 2


@pytest.mark.parametrize("name", ["scat_task_prepare", "terminal", "skill_manage"])
def test_unpublished_or_unrelated_operation_is_not_forwarded(guarded, name):
    bridge, _, registry, calls = guarded
    load(bridge)
    assert invoke(registry, name)["error"] == "skill_operation_not_allowed"
    assert calls == []


def test_history_control_read_keeps_its_own_authorization(guarded):
    _, _, registry, calls = guarded
    assert invoke(registry, "scat_task_get")["status"] == "ok"
    assert calls[0][0] == "scat_task_get"


def test_native_error_result_is_not_reported_as_business_success(guarded):
    bridge, guard, _, _ = guarded
    load(bridge)
    result = guard.invoke(
        lambda *a, **kw: json.dumps({"status": "error"}), "query", {}, session_id="session"
    )
    assert json.loads(result) == {"status": "error"}
    with sqlite3.connect(bridge.trace_path) as db:
        assert db.execute("SELECT outcome FROM skill_operations").fetchone()[0] == "returned"


def test_snapshot_tampering_stops_backend(guarded):
    bridge, _, registry, calls = guarded
    load(bridge)
    file = bridge.folder / "test-query/SKILL.md"
    file.chmod(0o600)
    file.write_text("tampered")
    assert invoke(registry)["error"] == "skill_snapshot_changed"
    assert calls == []


@pytest.mark.parametrize(
    "field,value", [("version", "other"), ("bundle_sha256", "other"), ("file_sha256", "other")]
)
def test_load_identity_mismatch_stops_backend(guarded, field, value):
    bridge, _, registry, calls = guarded
    load(bridge)
    with sqlite3.connect(bridge.trace_path) as db:
        db.execute(f"UPDATE skill_loads SET {field}=?", (value,))
    assert invoke(registry)["error"] == "skill_snapshot_mismatch"
    assert calls == []


def test_trace_failure_stops_before_backend(guarded):
    bridge, _, registry, calls = guarded
    load(bridge)
    with sqlite3.connect(bridge.trace_path) as db:
        db.execute("DROP TABLE skill_operations")
    assert invoke(registry)["error"] == "skill_operation_admission_unavailable"
    assert calls == []


def test_native_handler_refresh_does_not_bypass_dispatch_guard(guarded):
    bridge, guard, registry, calls = guarded
    assert invoke(registry)["error"] == "skill_required"
    # Updating the actual handler behind dispatch (as MCP rediscovery does)
    # cannot remove the registry dispatch boundary guard.
    guard.verify_installed()
    load(bridge)
    assert invoke(registry)["status"] == "ok"
    registry.dispatch = lambda *a, **kw: "{}"
    with pytest.raises(RuntimeError, match="unavailable"):
        guard.verify_installed()
    assert len(calls) == 1


def test_parallel_runs_do_not_share_loaded_skill(guarded):
    bridge, _, registry, calls = guarded
    bridge.bind("other", "owner:other-thread", "other-run")
    load(bridge)
    with ThreadPoolExecutor(max_workers=2) as pool:
        own = pool.submit(invoke, registry)
        other = pool.submit(invoke, registry, session="other")
    assert own.result()["status"] == "ok"
    assert other.result()["error"] == "skill_required"
    assert len(calls) == 1


def test_dispatch_exception_is_sanitized_and_audited(guarded):
    bridge, guard, _, _ = guarded
    load(bridge)

    def broken(*args, **kwargs):
        raise RuntimeError("private backend details")

    result = guard.invoke(broken, "query", {}, session_id="session")
    assert "private" not in result
    assert json.loads(result)["error"] == "skill_operation_dispatch_unavailable"
    with sqlite3.connect(bridge.trace_path) as db:
        assert db.execute("SELECT outcome FROM skill_operations").fetchone()[0] == "dispatch_error"


def test_configured_shared_trace_cap_never_evicts_active_evidence(guarded):
    from hermes_runtime.skill_policy import SkillPolicy, TracePolicy

    bridge, _, registry, calls = guarded
    bridge.policy = SkillPolicy(trace=TracePolicy(max_hot_records=2))
    load(bridge)
    assert invoke(registry)["status"] == "ok"
    assert invoke(registry)["error"] == "skill_operation_trace_capacity"
    assert len(calls) == 1
    result = json.loads(bridge.handler("skill_view")({"name": "test-query"}, session_id="session"))
    assert result["error"] == "skill_trace_capacity"
    with sqlite3.connect(bridge.trace_path) as db:
        assert db.execute("SELECT count(*) FROM skill_loads").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM skill_operations").fetchone()[0] == 1


def test_only_finished_idle_sessions_are_reclaimed(guarded, monkeypatch):
    from hermes_runtime import skills
    from hermes_runtime.skill_policy import SessionPolicy, SkillPolicy

    bridge, _, _, _ = guarded
    bridge.policy = SkillPolicy(sessions=SessionPolicy(max_active=1, idle_ttl_seconds=1))
    clock = [100.0]
    monkeypatch.setattr(skills.time, "time", lambda: clock[0])
    bridge.bind("session", "owner:thread", "run-1")
    clock[0] = 200
    with pytest.raises(ValueError, match="session_capacity"):
        bridge.bind("other", "owner:other", "run-2")
    bridge.finish_session("session")
    clock[0] = 202
    bridge.bind("other", "owner:other", "run-2")
    assert "session" not in bridge._sessions and "other" in bridge._sessions


def test_tool_budget_includes_skill_reads_and_failed_attempts(guarded):
    from hermes_runtime.skill_budget import RunBudget
    from hermes_runtime.skill_policy import Budget

    bridge, _, registry, calls = guarded
    bridge._budgets["session"] = RunBudget(
        bridge.trace_path, "owner:thread", "run-1", Budget(max_tool_calls=1), "p"
    )
    assert invoke(registry)["error"] == "skill_required"
    assert invoke(registry, name="skills_list")["error"] == "budget_exhausted:max_tool_calls"
    assert not calls
