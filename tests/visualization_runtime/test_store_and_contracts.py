"""Observable ownership, idempotency, lease fencing and legacy queue isolation."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from application.context import ExecutionContext
from application.task_store import TaskStore
from visualization_runtime.contracts import ReviewResult, VisualizationRequest
from visualization_runtime.store import JobConflict, JobNotFound, VisualizationJobStore


def request(**changes):
    return VisualizationRequest(
        **(
            {
                "intent": "Compare the actual sensor channels",
                "snapshot_id": "a" * 32,
                "snapshot_sha256": "b" * 64,
                "preferences_sha256": "c" * 64,
            }
            | changes
        )
    )


@pytest.fixture
def setup_store(tmp_path):
    now = [1000.0]
    store = VisualizationJobStore(
        tmp_path / "tasks.sqlite", workspace_root=tmp_path, clock=lambda: now[0]
    )
    return store, now, ExecutionContext("alice", "thread-1")


def test_idempotency_survives_restart_and_changed_request_is_rejected(setup_store, tmp_path):
    store, _, context = setup_store
    first = store.create(context, "request-1", request())
    restarted = VisualizationJobStore(store.path, workspace_root=tmp_path)
    assert restarted.create(context, "request-1", request())["task_id"] == first["task_id"]
    with pytest.raises(JobConflict, match="request_id_conflict"):
        store.create(context, "request-1", request(intent="Change the chart"))


def test_cross_user_and_cross_thread_are_hidden(setup_store):
    store, _, context = setup_store
    job = store.create(context, "request-1", request())
    for other in (ExecutionContext("bob", "thread-1"), ExecutionContext("alice", "thread-2")):
        with pytest.raises(JobNotFound):
            store.get(other, job["task_id"])
        with pytest.raises(JobNotFound):
            store.cancel(other, job["task_id"])


def test_only_one_worker_claims_a_job(setup_store):
    store, _, context = setup_store
    store.create(context, "request-1", request())
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(store.claim, ("worker-1", "worker-2")))
    assert sum(item is not None for item in results) == 1


def test_per_owner_and_global_admission(setup_store):
    store, _, context = setup_store
    store.create(context, "request-1", request())
    store.create(context, "request-2", request())
    store.create(ExecutionContext("bob", "thread-2"), "request-1", request())
    first, second = store.claim("w1"), store.claim("w2")
    assert first["owner"] != second["owner"]
    assert store.claim("w3") is None


def test_expired_lease_and_old_generation_cannot_write(setup_store):
    store, now, context = setup_store
    job = store.create(context, "request-1", request())
    claimed = store.claim("worker", lease_seconds=10)
    with pytest.raises(JobConflict, match="lease_lost"):
        store.heartbeat(job["task_id"], "worker", claimed["generation"] + 1)
    now[0] += 11
    assert store.get(context, job["task_id"])["state"] == "needs_reconciliation"
    with pytest.raises(JobConflict, match="lease_lost"):
        store.fail(job["task_id"], "worker", claimed["generation"], error_code="render_failed")


def test_heartbeat_extends_only_current_lease(setup_store):
    store, now, context = setup_store
    job = store.create(context, "request-1", request())
    claimed = store.claim("worker", lease_seconds=10)
    now[0] += 9
    store.heartbeat(job["task_id"], "worker", claimed["generation"], lease_seconds=10)
    now[0] += 2
    assert store.get(context, job["task_id"])["state"] == "running"


def test_running_cancel_fences_worker_but_retains_resource_slot(setup_store):
    store, _, context = setup_store
    job = store.create(context, "request-1", request())
    claimed = store.claim("worker")
    cancelled = store.cancel(context, job["task_id"])
    assert cancelled["state"] == "needs_reconciliation"
    assert cancelled["generation"] == claimed["generation"] + 1
    assert store.cancel(context, job["task_id"])["generation"] == cancelled["generation"]
    with pytest.raises(JobConflict, match="lease_lost"):
        store.transition(
            job["task_id"],
            "worker",
            claimed["generation"],
            expected_phase="generating",
            phase="rendering",
        )
    store.create(context, "request-2", request())
    assert store.claim("worker-2") is None


def test_queued_cancel_and_failed_job_are_terminal(setup_store):
    store, _, context = setup_store
    job = store.create(context, "request-1", request())
    assert store.cancel(context, job["task_id"])["state"] == "cancelled"
    assert store.claim("worker") is None
    store.create(context, "request-2", request())
    claimed = store.claim("worker")
    store.fail(claimed["task_id"], "worker", claimed["generation"], error_code="render_failed")
    assert store.get(context, claimed["task_id"])["state"] == "failed"


def test_phase_cannot_skip_review_and_no_success_api_exists(setup_store):
    store, _, context = setup_store
    job = store.create(context, "request-1", request())
    claimed = store.claim("worker")
    with pytest.raises(JobConflict, match="phase_transition"):
        store.transition(
            job["task_id"],
            "worker",
            claimed["generation"],
            expected_phase="generating",
            phase="publishing",
        )
    store.transition(
        job["task_id"],
        "worker",
        claimed["generation"],
        expected_phase="generating",
        phase="rendering",
    )
    assert store.get(context, job["task_id"])["phase"] == "rendering"
    assert not hasattr(store, "succeed")


def test_legacy_unknown_does_not_block_visualization_and_is_not_changed(setup_store, tmp_path):
    store, _, context = setup_store
    legacy = TaskStore(store.path, workspace_root=tmp_path)
    legacy.prepare(context.scope_key, "old-request", {"run": {"run_id": "e" * 32}, "proposal": {}})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE model_tasks SET state='unknown'")
        before = db.execute("SELECT * FROM model_tasks").fetchall()
    store.create(context, "request-1", request())
    assert store.claim("worker") is not None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT * FROM model_tasks").fetchall() == before


def test_visualization_unknown_does_not_block_legacy_claim(setup_store, tmp_path):
    store, _, context = setup_store
    job = store.create(context, "request-1", request())
    store.claim("drawing-worker")
    store.cancel(context, job["task_id"])
    legacy = TaskStore(store.path, workspace_root=tmp_path)
    row = legacy.prepare(
        context.scope_key, "legacy-request", {"run": {"run_id": "e" * 32}, "proposal": {}}
    )
    legacy.confirm(context.scope_key, row["task_id"], row["sha256"])
    assert legacy.claim("legacy-worker")["task_id"] == row["task_id"]


def test_request_corruption_is_rejected(setup_store):
    store, _, context = setup_store
    job = store.create(context, "request-1", request())
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE visualization_jobs SET request='{}'")
    with pytest.raises(JobConflict, match="integrity_failed"):
        store.get(context, job["task_id"])
    with pytest.raises(JobConflict, match="integrity_failed"):
        store.claim("worker")


def test_future_schema_rejected_without_creating_tables(tmp_path):
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE visualization_schema(version INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO visualization_schema VALUES(99)")
    with pytest.raises(JobConflict, match="unsupported_visualization_schema"):
        VisualizationJobStore(path, workspace_root=tmp_path)
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='visualization_jobs'").fetchone()
            is None
        )


def test_state_path_cannot_escape_workspace(tmp_path):
    with pytest.raises(ValueError, match="outside_workspace"):
        VisualizationJobStore(tmp_path.parent / "escaped.sqlite", workspace_root=tmp_path)


def test_contracts_reject_extra_authority_and_inconsistent_review():
    with pytest.raises(ValidationError):
        request(owner="someone-else")
    with pytest.raises(ValidationError):
        request(parent_artifact_id="d" * 32)
    with pytest.raises(ValidationError):
        request(viewports=("desktop", "desktop"))
    issue = {
        "severity": "major",
        "location": "axis",
        "message": "Missing units",
        "suggestion": "Add the source unit",
    }
    with pytest.raises(ValidationError):
        ReviewResult(decision="pass", issues=(issue,), checked_viewports=("desktop",))
    with pytest.raises(ValidationError):
        ReviewResult(decision="revise", checked_viewports=("desktop",))
    assert (
        ReviewResult(
            decision="revise", issues=(issue,), checked_viewports=("desktop", "mobile")
        ).decision
        == "revise"
    )
