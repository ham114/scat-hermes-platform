from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from application.context import ExecutionContext
from application.model_worker import run_once
from application.task_store import TaskConflict, TaskNotFound, TaskStore


@pytest.fixture
def store(tmp_path):
    clock = SimpleNamespace(now=1000.0)
    db = TaskStore(tmp_path / "tasks.sqlite", workspace_root=tmp_path, clock=lambda: clock.now)
    return db, clock


def proposal(run="a" * 32, value=1):
    return {"proposal": {"value": value}, "run": {"run_id": run}}


def prepare(db, scope=None, key="one", run="a" * 32, value=1):
    scope = scope or ExecutionContext("owner", "thread").scope_key
    return db.prepare(scope, key, proposal(run, value))


def test_confirmation_expires_and_cannot_cross_user_or_thread(store):
    db, clock = store
    context = ExecutionContext("owner", "thread")
    row = prepare(db)
    for context2 in [ExecutionContext("other", "thread"), ExecutionContext("owner", "other")]:
        with pytest.raises(TaskNotFound):
            db.confirm(context2.scope_key, row["task_id"], row["sha256"])
    clock.now += 901
    with pytest.raises(TaskConflict):
        db.confirm(context.scope_key, row["task_id"], row["sha256"])
    assert db.get(context.scope_key, row["task_id"])["state"] == "expired"
    assert db.claim("worker") is None


def test_prepare_and_confirm_idempotency_with_parameter_digest(store):
    db, _ = store
    row = prepare(db)
    assert prepare(db, run="b" * 32)["task_id"] == row["task_id"]
    with pytest.raises(TaskConflict):
        prepare(db, value=2)
    with pytest.raises(TaskConflict):
        db.confirm(row["scope"], row["task_id"], "0" * 64)
    assert db.claim("worker") is None
    first = db.confirm(row["scope"], row["task_id"], row["sha256"])
    second = db.confirm(row["scope"], row["task_id"], row["sha256"])
    assert first["run_id"] == second["run_id"] == "a" * 32
    assert first["state"] == second["state"] == "queued"
    assert len(db.list(row["scope"])) == 1


def test_revocation_and_concurrent_claim_single_execution(store):
    db, _ = store
    row = prepare(db)
    db.confirm(row["scope"], row["task_id"], row["sha256"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda n: db.claim(str(n)), range(8)))
    claimed = [x for x in rows if x]
    assert len(claimed) == 1
    with pytest.raises(TaskConflict):
        db.revoke(row["scope"], row["task_id"])
    with pytest.raises(TaskConflict):
        db.finish(row["task_id"], "wrong-worker")
    db.finish(row["task_id"], claimed[0]["worker"])
    assert db.claim("retry") is None
    assert db.confirm(row["scope"], row["task_id"], row["sha256"])["state"] == "finished"


def test_worker_crash_becomes_unknown_and_blocks_all_further_execution(store):
    db, clock = store
    first = prepare(db)
    second = prepare(db, key="two", run="b" * 32)
    for row in [first, second]:
        db.confirm(row["scope"], row["task_id"], row["sha256"])
    assert db.claim("dead-worker")["task_id"] == first["task_id"]
    clock.now += 61
    reloaded = TaskStore(db.path, workspace_root=db.path.parent, clock=lambda: clock.now)
    assert reloaded.get(first["scope"], first["task_id"])["state"] == "unknown"
    assert reloaded.claim("new-worker") is None
    with pytest.raises(TaskConflict):
        db.heartbeat(first["task_id"], "dead-worker")
    assert db.confirm(first["scope"], first["task_id"], first["sha256"])["state"] == "unknown"
    db.reconcile_terminal(first["task_id"], run_id=first["run_id"])
    assert reloaded.claim("next")["task_id"] == second["task_id"]


def test_revoked_confirmations_cannot_replay_and_tampering_is_detected(store):
    db, _ = store
    row = prepare(db)
    db.revoke(row["scope"], row["task_id"])
    with pytest.raises(TaskConflict):
        db.confirm(row["scope"], row["task_id"], row["sha256"])
    with db.connect() as c:
        c.execute("UPDATE model_tasks SET snapshot=? WHERE task_id=?", ("{}", row["task_id"]))
    with pytest.raises(TaskConflict):
        db.get(row["scope"], row["task_id"])


def test_worker_heartbeat_requires_unexpired_ownership(store):
    db, clock = store
    row = prepare(db)
    db.confirm(row["scope"], row["task_id"], row["sha256"])
    db.claim("worker")
    clock.now += 59
    db.heartbeat(row["task_id"], "worker")
    clock.now += 59
    assert db.get(row["scope"], row["task_id"])["state"] == "claimed"
    clock.now += 2
    with pytest.raises(TaskConflict):
        db.finish(row["task_id"], "worker")
    assert db.get(row["scope"], row["task_id"])["state"] == "unknown"


def test_path_and_input_limits(store, tmp_path):
    db, _ = store
    with pytest.raises(ValueError):
        TaskStore(tmp_path.parent / "outside.sqlite", workspace_root=tmp_path)
    with pytest.raises(ValueError):
        db.prepare("model-supplied-user", "key", proposal())
    scope = ExecutionContext("owner", "thread").scope_key
    for n in range(50):
        prepare(db, key=str(n), run=f"{n:032x}")
    with pytest.raises(TaskConflict):
        prepare(db, key="overflow", run="f" * 32)
    assert len(db.list(scope)) == 50
    with pytest.raises(ValueError):
        db.list(scope, limit=51)


@pytest.mark.asyncio
async def test_independent_worker_finishes_once_and_failure_does_not_retry(store):
    db, _ = store
    row = prepare(db)
    db.confirm(row["scope"], row["task_id"], row["sha256"])
    calls = []

    async def execute(app, task):
        calls.append(task["run_id"])
        return True, None

    app = SimpleNamespace(store=db)
    assert await run_once(app, "worker", execute=execute)
    assert not await run_once(app, "worker", execute=execute)
    assert calls == [row["run_id"]]
    second = prepare(db, key="failure", run="b" * 32)
    db.confirm(second["scope"], second["task_id"], second["sha256"])

    async def fail(app, task):
        raise OSError("private-value")

    assert await run_once(app, "worker", execute=fail)
    assert db.get(second["scope"], second["task_id"])["state"] == "unknown"
    assert not await run_once(app, "worker", execute=execute)


@pytest.mark.asyncio
async def test_interrupt_does_not_requeue_or_lose_durable_task(store):
    import asyncio

    db, _ = store
    row = prepare(db)
    db.confirm(row["scope"], row["task_id"], row["sha256"])
    started = asyncio.Event()

    async def execute(app, task):
        started.set()
        await asyncio.Event().wait()

    work = asyncio.create_task(run_once(SimpleNamespace(store=db), "worker", execute=execute))
    await started.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert db.get(row["scope"], row["task_id"])["state"] == "unknown"
    assert db.claim("new-worker") is None
