"""Approved CPU jobs: no LLM/DB/network required for lifecycle and fencing tests."""

import time
from pathlib import Path
from uuid import uuid4

import pytest

from application.context import ExecutionContext
from application.model_step_store import ModelStepStore, StepError
from application.model_tasks import TaskProposal
from application.skill_job_factory import create_skill_tasks
from application.skill_worker import SkillWorker, completion_reference
from application.task_store import TaskConflict, canonical, digest
from hermes_runtime.job_budget import JobBudget, ensure_account
from hermes_runtime.job_context import load_approved_job
from hermes_runtime.skill_budget import BudgetExhausted, RunBudget
from hermes_runtime.skill_policy import Budget
from scat_model_runtime import ModelTaskRequest

ROOT = Path(__file__).resolve().parents[2]


class Runs:
    def __init__(self):
        self.rows = {}

    async def get_for_owner(self, run_id, *, owner_user_id):
        row = self.rows.get(run_id)
        return row if row and row.owner_user_id == owner_user_id else None

    async def create(self, row):
        self.rows[row.run_id] = row

    async def transition(self, run_id, *, expected_statuses, status, progress, **kwargs):
        row = self.rows[run_id]
        assert row.status in expected_statuses
        self.rows[run_id] = row.model_copy(
            update={
                "status": status,
                "progress": progress,
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k
                    in {"result_summary", "result_artifact_ids", "result_schema_version", "error"}
                },
            }
        )


class Artifacts:
    def __init__(self):
        self.rows = {}

    async def get_for_owner(self, identity, *, owner_user_id):
        row = self.rows.get(identity)
        return row if row and row.owner_user_id == owner_user_id else None

    async def create(self, row):
        assert row.artifact_id not in self.rows
        self.rows[row.artifact_id] = row


@pytest.fixture
def app(tmp_path):
    return create_skill_tasks(
        ROOT, tmp_path / "jobs.sqlite", cpu_fixture=True, runs=Runs(), artifacts=Artifacts()
    )


def prepare(app, *, budget=None, source_run=None, context=None):
    context = context or ExecutionContext("scat-researcher", uuid4().hex)
    _, grant = app.skills.kernel()
    request = ModelTaskRequest.model_validate_json(grant.request_json)
    proposal = TaskProposal(**{name: getattr(request, name) for name in TaskProposal.model_fields})
    return context, app.prepare(
        context, proposal, proposal_key=uuid4().hex, source_run_id=source_run, budget=budget
    )


def test_native_job_binding_requires_current_lease(app):
    _, row = approved(app)
    claimed = app.skills.store.claim("native-worker")
    store, bound_row = load_approved_job(
        ROOT, app.store.path, row["task_id"], "native-worker", claimed["job"]["generation"]
    )
    assert store.path == app.store.path and bound_row["sha256"] == row["sha256"]
    with pytest.raises(TaskConflict):
        load_approved_job(ROOT, app.store.path, row["task_id"], "forged-worker", 1)


def approved(app, *, budget=None):
    context, view = prepare(app, budget=budget)
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    return context, app.skills.store.get(context.scope_key, view["task_id"])


def bound(app, row, *, worker="worker-a", parent=None, operations=None):
    child = ExecutionContext("scat-researcher", "child-" + uuid4().hex)
    ops = operations or row["snapshot"]["skills"]["operations"]
    delegate = app.skills.store.delegate(
        row["scope"], row["task_id"], child.scope_key, ops, parent=parent
    )
    identity = app.skills.store.bind(
        delegate,
        child.scope_key,
        "run_" + uuid4().hex,
        uuid4().hex,
        worker,
        row["job"]["generation"],
    )
    return identity, app.skills.store.session(identity), delegate


@pytest.mark.asyncio
async def test_unconfirmed_proposal_has_no_job_or_step_execution(app):
    context, view = prepare(app)
    assert view["confirmation_required"] and not view["skill_job"]["approved"]
    assert app.skills.store.claim("worker") is None
    with pytest.raises(TaskConflict):
        app.skills.store.bind("forged", context.scope_key, "run_a", "native", "worker", 1)
    with app.store.connect() as db:
        assert db.execute("SELECT count(*) FROM skill_backend_executions").fetchone()[0] == 0


def test_same_confirmation_one_job_and_changed_sha_denied(app):
    context, view = prepare(app)
    for _ in range(3):
        app.confirm(context, view["task_id"], view["snapshot_sha256"])
    with pytest.raises(TaskConflict):
        app.confirm(context, view["task_id"], "0" * 64)
    with app.store.connect() as db:
        assert db.execute("SELECT count(*) FROM skill_jobs").fetchone()[0] == 1
    row = app.skills.store.claim("worker-a")
    assert row and app.skills.store.claim("worker-b") is None


def test_legacy_worker_cannot_claim_skill_jobs(app):
    approved(app)
    assert app.store.claim("legacy-worker") is None
    assert app.skills.store.claim("skill-worker") is not None


@pytest.mark.asyncio
async def test_result_publication_conflict_does_not_leave_an_active_worker(app, monkeypatch):
    context, row = approved(app)

    async def engine(*args):
        pass

    async def conflict(*args, **kwargs):
        raise TaskConflict("Saved result conflicts with approved job")

    monkeypatch.setattr(
        "application.skill_worker.completion_reference", lambda *args: {"artifact_id": "verified"}
    )
    monkeypatch.setattr("application.skill_worker.publish_result", conflict)
    assert await SkillWorker(app, engine).once()
    final = app.skills.store.get(context.scope_key, row["task_id"])
    assert final["job"]["state"] == "unknown"
    assert final["job"]["error"] == "result_publish_conflict_requires_reconciliation"


def test_completed_backend_hash_checked_without_pending_steps(app):
    context, row = approved(app)
    app.skills.store.claim("worker-a")
    with app.store.connect() as db:
        db.execute(
            "INSERT INTO skill_backend_executions(id,task_id,step_key,generation,worker,state,request,request_sha,output,output_sha) VALUES('done',?,'step',1,'worker-a','succeeded','{}',?,'{}',?)",
            (row["task_id"], digest({}), "0" * 64),
        )
    app.skills.store.state(row["scope"], row["task_id"], "unknown")
    with pytest.raises(TaskConflict, match="integrity"):
        app.skills.reconcile(context, row["task_id"])
    assert app.skills.store.get(context.scope_key, row["task_id"])["job"]["state"] == "unknown"


def test_lease_generation_fences_old_worker_after_recovery(app):
    context, row = approved(app)
    first = app.skills.store.claim("old", lease_seconds=1)
    identity, _session, _ = bound(app, first, worker="old")

    def clock():
        return time.time() + 3

    app.store.clock = clock
    app.skills.store.clock = clock
    assert app.skills.store.get(context.scope_key, row["task_id"])["job"]["state"] == "unknown"
    app.skills.reconcile(context, row["task_id"])
    second = app.skills.store.claim("new")
    assert second["job"]["generation"] > first["job"]["generation"]
    with pytest.raises(TaskConflict):
        app.skills.store.session(identity)
    with pytest.raises(TaskConflict):
        app.skills.store.heartbeat(row["task_id"], "old", first["job"]["generation"])
    with pytest.raises(TaskConflict):
        app.skills.store.state(
            row["scope"],
            row["task_id"],
            "succeeded",
            worker="old",
            generation=first["job"]["generation"],
        )


@pytest.mark.asyncio
async def test_scoped_handoff_and_successful_input_reuse(app):
    context, row = approved(app)
    row = app.skills.store.claim("worker-a")
    identity, session, _ = bound(app, row)

    async def invoke(name, args):
        return await app.skills.invoke(
            identity, name, args, scope=session["scope"], native_run=session["native_run"]
        )

    first = await invoke("scat_model_prepare_input", {})
    second = await invoke("scat_model_prepare_input", {})
    assert first["reference"] == second["reference"] and second["reused"]
    with pytest.raises(TaskConflict):
        await app.skills.invoke(
            identity,
            "scat_model_prepare_input",
            {},
            scope=context.scope_key,
            native_run=session["native_run"],
        )
    with pytest.raises(StepError):
        await invoke("scat_model_prepare_input", {"confirmed": True})
    with pytest.raises(StepError):
        await invoke("scat_model_evaluate", {"reference": first["reference"]})
    other_context, other = prepare(app)
    app.confirm(other_context, other["task_id"], other["snapshot_sha256"])
    with pytest.raises(StepError, match="not_found"):
        ModelStepStore(app.store.path, workspace_root=ROOT).read(
            other_context.scope_key, other["run_id"], first["reference"], kind="input"
        )


def test_nested_delegations_cannot_increase_capability_or_depth(app):
    _, row = approved(app)
    row = app.skills.store.claim("worker-a")
    _, _, parent = bound(app, row, operations=["scat_model_prepare_input"])
    with pytest.raises(TaskConflict):
        bound(app, row, parent=parent, operations=["scat_model_run"])
    child_scope = ExecutionContext("scat-researcher", "nested").scope_key
    with pytest.raises(TaskConflict):
        app.skills.store.delegate(
            row["scope"],
            row["task_id"],
            child_scope,
            ["scat_model_prepare_input"],
            parent=parent,
            max_depth=1,
        )
    identity, _, _ = bound(app, row, parent=parent, operations=["scat_model_prepare_input"])
    with app.store.connect() as db:
        db.execute("UPDATE skill_job_delegations SET revoked=1 WHERE id=?", (parent,))
    with pytest.raises(TaskConflict):
        app.skills.store.session(identity)


def test_child_sessions_share_job_budget_across_restart(app):
    _, row = approved(app, budget={"max_tool_calls": 3})
    row = app.skills.store.claim("worker-a")
    _, a, _ = bound(app, row)
    _, b, _ = bound(app, row)
    first = JobBudget(app.skills.store, row, a)
    second = JobBudget(app.skills.store, row, b)
    first.admit_tool()
    second.admit_tool()
    first.finish()
    restored = JobBudget(app.skills.store, row, b)
    restored.admit_tool()
    with pytest.raises(BudgetExhausted):
        restored.admit_tool()
    assert first.snapshot()["tool_calls"] == 3 and first.snapshot()["finished_at"] is None
    assert restored.snapshot()["exhausted"] == "max_tool_calls"


def test_parent_planning_usage_imported_once_only_after_finish(app, tmp_path):
    ledger = tmp_path / "planner.sqlite"
    app.skills.parent_budget_path = ledger
    context = ExecutionContext("scat-researcher", "planner")
    parent = RunBudget(ledger, context.scope_key, "run_parent", Budget(), "parent-policy")
    parent.admit_tool()
    parent.reserve_llm({"messages": [{"content": "synthetic"}]})
    _, view = prepare(app, source_run="run_parent", context=context)
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    assert app.skills.store.claim("child") is None
    parent.admit_tool()
    parent.finish()
    app.skills.ready_parents()
    app.skills.ready_parents()
    row = app.skills.store.claim("child")
    usage = ensure_account(app.skills.store, row).snapshot()
    assert usage["tool_calls"] == 2 and usage["llm_calls"] == 1 and usage["charged_tokens"] > 0


def test_budget_revision_preserves_usage_and_requires_pause(app):
    context, row = approved(app, budget={"max_tool_calls": 2})
    row = app.skills.store.claim("worker-a")
    _, session, _ = bound(app, row)
    budget = JobBudget(app.skills.store, row, session)
    budget.admit_tool()
    budget.admit_tool()
    with pytest.raises(BudgetExhausted):
        budget.admit_tool()
    with pytest.raises(TaskConflict):
        app.skills.revise_budget(context, row["task_id"], {"max_tool_calls": 4})
    app.skills.store.state(row["scope"], row["task_id"], "paused")
    with pytest.raises(ValueError):
        app.skills.revise_budget(context, row["task_id"], {"max_tool_calls": 1000000})
    revised = app.skills.revise_budget(context, row["task_id"], {"max_tool_calls": 4})
    assert revised["job"]["budget_revision"] == 1 and revised["job"]["state"] == "queued"
    assert ensure_account(app.skills.store, revised).snapshot()["tool_calls"] == 2
    with pytest.raises(BudgetExhausted):
        budget.admit_tool()


@pytest.mark.parametrize(
    "stage", ["running_without_backend", "backend_prepared", "backend_completed"]
)
def test_recovery_requires_evidence_and_never_changes_execution_id(app, stage):
    context, row = approved(app)
    row = app.skills.store.claim("worker-a")
    steps = ModelStepStore(app.store.path, workspace_root=ROOT)
    key, _ = steps.reserve(row["scope"], row["run_id"], "scat_model_run", {"reference": "fixed"})
    identity = digest({"job": row["task_id"], "operation": "scat_model_run"})
    if stage != "running_without_backend":
        with app.store.connect() as db:
            output = {"synthetic": True}
            db.execute(
                "INSERT INTO skill_backend_executions(id,task_id,step_key,generation,worker,state,request,request_sha,output,output_sha,calls) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    row["task_id"],
                    key,
                    row["job"]["generation"],
                    "worker-a",
                    "succeeded" if stage == "backend_completed" else "prepared",
                    "{}",
                    digest({}),
                    canonical(output),
                    digest(output),
                    1 if stage == "backend_completed" else 0,
                ),
            )
    app.skills.store.state(row["scope"], row["task_id"], "unknown")
    recovered = app.skills.reconcile(context, row["task_id"])
    assert recovered["job"]["state"] == "queued"
    with app.store.connect() as db:
        assert db.execute("SELECT state FROM model_step_receipts").fetchone()[0] == "prepared"
        executions = db.execute("SELECT id,calls FROM skill_backend_executions").fetchall()
    assert not executions or executions[0][0] == identity
    if stage == "backend_completed":
        assert executions[0][1] == 1


@pytest.mark.parametrize("backend_state", ["running", "unknown"])
def test_uncertain_backend_holds_resource_slot_and_refuses_resume(app, backend_state):
    context, row = approved(app)
    row = app.skills.store.claim("worker-a")
    with app.store.connect() as db:
        db.execute(
            "INSERT INTO skill_backend_executions(id,task_id,step_key,generation,worker,state,request,request_sha) VALUES('external',?,'step',1,'worker-a',?,'{}',?)",
            (row["task_id"], backend_state, digest({})),
        )
    app.skills.store.state(row["scope"], row["task_id"], "unknown")
    assert app.skills.reconcile(context, row["task_id"])["job"]["state"] == "unknown"
    context2, next_task = prepare(app)
    app.confirm(context2, next_task["task_id"], next_task["snapshot_sha256"])
    assert app.skills.store.claim("worker-b") is None


def test_cancel_request_fences_tools_before_backend_stop_is_acknowledged(app):
    context, row = approved(app)
    row = app.skills.store.claim("worker-a")
    identity, _, _ = bound(app, row)
    requested = app.skills.store.cancel(context.scope_key, row["task_id"])
    assert requested["job"]["state"] == "cancel_requested"
    with pytest.raises(TaskConflict):
        app.skills.store.session(identity)
    reconciled = app.skills.reconcile(context, row["task_id"])
    assert reconciled["job"]["state"] == "cancelled"


def test_changed_skill_or_input_snapshot_cannot_start_new_effect(app):
    _context, row = approved(app)
    row["snapshot"]["skills"]["bundle_sha256"] = "0" * 64
    with pytest.raises(TaskConflict):
        app.skills.validate(row)


@pytest.mark.asyncio
async def test_worker_does_not_trust_textual_success_without_step_receipts(app):
    context, row = approved(app)

    async def fake(*args):
        return {"status": "completed", "output": "all succeeded"}

    await SkillWorker(app, fake).once()
    assert not app.artifacts.rows
    assert completion_reference(app, row) is None
    assert app.skills.store.get(context.scope_key, row["task_id"])["job"]["state"] != "succeeded"
