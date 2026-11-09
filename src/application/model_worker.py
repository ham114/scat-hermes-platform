"""Independent executor adapter around the existing deterministic model graph."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from application.task_store import TaskConflict, digest

logger = logging.getLogger(__name__)


async def execute_claimed(app, row):
    """Never retry an already claimed run, including failures persisting its receipt."""
    if "skills" in row["snapshot"]:
        raise TaskConflict("skill_job_requires_skill_worker")
    from agents.model_tools import _execute_persisted_model_run
    from analysis_runs import AnalysisRunError, AnalysisRunRecord
    from scat_model_runtime import ModelRegistryError, ModelRunStatus, ModelTaskRequest

    snapshot = row["snapshot"]
    record = AnalysisRunRecord.model_validate(snapshot["run"])
    existing = await app.runs.get_for_owner(record.run_id, owner_user_id=record.owner_user_id)
    if existing is not None:
        return False, "existing_run_requires_reconciliation"
    request = ModelTaskRequest.model_validate(snapshot["request"]).model_copy(
        update={"confirmed": True}
    )
    try:
        current = app.models.match(request)
        changed = digest(current.model_dump(mode="json")) != digest(snapshot["spec"])
    except ModelRegistryError:
        changed = True
    await app.runs.create(record)
    if changed:
        await app.runs.transition(
            record.run_id,
            expected_statuses=(ModelRunStatus.QUEUED,),
            status=ModelRunStatus.FAILED,
            progress=1.0,
            error=AnalysisRunError(
                code="registration_changed_before_execution",
                message="模型注册已变化，任务未执行；请重新准备并确认。",
            ),
        )
        return True, "registration_changed_before_execution"
    try:
        await _execute_persisted_model_run(request, record)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- uncertain execution must never be retried
        # Heavy execution may have started. Never assert failure/retry on this path.
        logger.warning("model_worker_execution_unresolved run_id=%s", record.run_id)
        return False, "execution_outcome_unknown"
    final = await app.runs.get_for_owner(record.run_id, owner_user_id=record.owner_user_id)
    terminal = {ModelRunStatus.SUCCEEDED, ModelRunStatus.FAILED, ModelRunStatus.CANCELLED}
    return (
        (True, None) if final and final.status in terminal else (False, "terminal_receipt_missing")
    )


async def run_once(app, worker_id, *, execute=execute_claimed, heartbeat_interval=5):
    row = await asyncio.to_thread(app.store.claim, worker_id)
    if row is None:
        return False
    lost = asyncio.Event()

    async def heartbeat():
        while True:
            await asyncio.sleep(heartbeat_interval)
            try:
                await asyncio.to_thread(app.store.heartbeat, row["task_id"], worker_id)
            except Exception:  # noqa: BLE001 -- uncertain execution must never be retried
                lost.set()
                logger.warning("model_worker_lease_unavailable task_id=%s", row["task_id"])
                return

    pulse = asyncio.create_task(heartbeat())
    try:
        try:
            terminal, code = await execute(app, row)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    app.store.finish,
                    row["task_id"],
                    worker_id,
                    unknown=True,
                    error_code="worker_interrupted",
                )
            raise
        except Exception:  # noqa: BLE001 -- uncertain execution must never be retried
            terminal, code = False, "worker_outcome_unknown"
            logger.warning("model_worker_unresolved task_id=%s", row["task_id"])
        if not lost.is_set():
            await asyncio.to_thread(
                app.store.finish, row["task_id"], worker_id, unknown=not terminal, error_code=code
            )
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse
    return True


async def reconcile(app, context, task_id):
    from scat_model_runtime import ModelRunStatus

    row = app.store.get(context.scope_key, task_id)
    record = await app.runs.get_for_owner(row["run_id"], owner_user_id=context.user_id)
    if (
        record is None
        or record.thread_id != context.thread_id
        or record.status
        not in {ModelRunStatus.SUCCEEDED, ModelRunStatus.FAILED, ModelRunStatus.CANCELLED}
    ):
        raise TaskConflict("No verified terminal run; do not re-execute")
    app.store.reconcile_terminal(task_id, run_id=record.run_id)
    return await app.get(context, task_id)
