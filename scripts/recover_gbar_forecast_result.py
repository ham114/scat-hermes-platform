"""Owned recovery: normalize null truth from the original completed GPU receipt; never POST."""

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

ROOT = Path.cwd()
TASK = "8121b9d0764e4ff584d4146fe2b57a39"


async def main():
    from application.context import ExecutionContext
    from application.skill_cpu_backend import process_start
    from application.skill_job_factory import create_skill_tasks
    from application.skill_worker import completion_reference, publish_result
    from application.task_store import canonical, digest
    from hermes_runtime.job_budget import ensure_account
    from scat.infrastructure.database import flight_database
    from scat_model_runtime.backends.base import InferenceResult
    from workflow_runtime.data import load_input
    from workflow_runtime.gpu import GPURequests, run_gpu_trial

    state = ROOT / os.environ["HERMES_TASK_STATE_FILE"]
    app = create_skill_tasks(ROOT, state, parent_budget_path=os.environ["SCAT_PARENT_BUDGET_PATH"])
    with app.store.connect() as db:
        raw = db.execute("SELECT * FROM model_tasks WHERE task_id=?", (TASK,)).fetchone()
        assert raw
        row = app.skills.store.get(raw["scope"], TASK)
        assert row["job"]["state"] == "unknown"
        assert (
            db.execute(
                "SELECT count(*) FROM model_tasks WHERE task_id<>? AND state IN ('queued','claimed','running','unknown')",
                (TASK,),
            ).fetchone()[0]
            == 0
        )
        execution = db.execute(
            "SELECT * FROM skill_backend_executions WHERE task_id=?", (TASK,)
        ).fetchall()
        assert len(execution) == 1
        execution = dict(execution[0])
        assert (
            execution["state"] == "unknown"
            and execution["error"] == "gpu_unapproved_future_truth"
            and execution["calls"] == 1
        )
        assert process_start(execution["pid"]) != execution["process_start"]
        assert digest(row["snapshot"]) == row["sha256"]
        assert row["snapshot"]["skills"]["sources"] == app.skills.sources()
        budget = dict(
            db.execute(
                "SELECT * FROM skill_run_budgets WHERE run_id=?", (row["run_id"],)
            ).fetchone()
        )
        assert not budget["exhausted"] and budget["finished_at"] is None
    directory = ROOT / "var/validation/20261108-gbar-live"
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / "private-task-before-null-truth-recovery.sqlite"
    if backup.exists():
        with sqlite3.connect("file:" + str(backup) + "?mode=ro", uri=True) as previous:
            assert (
                previous.execute(
                    "SELECT sha256 FROM model_tasks WHERE task_id=?", (TASK,)
                ).fetchone()[0]
                == row["sha256"]
            )
            assert previous.execute(
                "SELECT state,request_sha FROM skill_backend_executions WHERE id=?",
                (execution["id"],),
            ).fetchone() == ("unknown", execution["request_sha"])
    else:
        with sqlite3.connect(state) as source, sqlite3.connect(backup) as target:
            source.backup(target)
    backup.chmod(0o600)
    plan = row["snapshot"]["workflow"]
    frozen = load_input(ROOT, plan["input_sha256"], scope=row["scope"])
    assert plan["proposal"]["kind"] == "forecast" and plan["proposal"]["observation_last_n"] == 200
    assert len(plan["trials"]) == len(frozen["flights"]) == 1
    window = frozen["flights"][0]["gbar_windows"][0]
    assert (
        len(window["timestamps"]) == 200 and not window["annotations"]["actual_future_candidates"]
    )
    used = []

    def cached_only(ledger, backend, request, *, task, execution, authorize=lambda: None):
        assert task == TASK
        with ledger.connect() as db:
            saved = db.execute(
                "SELECT * FROM gpu_requests WHERE id=?", (request.request_id,)
            ).fetchone()
        assert saved and saved["state"] == "succeeded" and saved["calls"] == 1
        assert (
            saved["task"] == task
            and saved["execution"] == execution
            and saved["request_sha"] == digest(request.model_dump(mode="json"))
        )
        value = json.loads(saved["output"])
        assert digest(value) == saved["output_sha"]
        result = InferenceResult.model_validate(value)
        result.verify_identity(request)
        for series in value["payload"]["series"]:
            assert series["expected_values"] and all(
                v is None for r in series["expected_values"] for v in r
            )
            assert not series["annotations"].get("actual_future_trajectory")
            series["expected_values"] = []
        used.append(
            {"request_id": request.request_id, "original_output_sha256": saved["output_sha"]}
        )
        return InferenceResult.model_validate(value)

    original = GPURequests.execute
    GPURequests.execute = cached_only
    try:
        output = await asyncio.to_thread(
            run_gpu_trial, ROOT, row, execution["id"], plan["trials"][0], frozen
        )
    finally:
        GPURequests.execute = original
    assert (
        len(used) == 1
        and output["status"] == "succeeded"
        and not output["samples"]
        and output["prediction_count"] == 3
    )
    with app.skills.store.transaction() as db:
        worker = "operator-gbar-receipt-recovery"
        generation = row["job"]["generation"] + 1
        now = app.store.clock()
        assert (
            db.execute(
                "UPDATE skill_jobs SET state='running',worker=?,generation=?,lease=?,updated=?,error=NULL WHERE task_id=? AND state='unknown' AND generation=?",
                (worker, generation, now + 300, now, TASK, generation - 1),
            ).rowcount
            == 1
        )
        assert (
            db.execute(
                "UPDATE model_tasks SET state='claimed',worker=?,lease=?,updated=?,error_code=NULL WHERE task_id=? AND state='unknown' AND sha256=?",
                (worker, now + 300, now, TASK, row["sha256"]),
            ).rowcount
            == 1
        )
        assert (
            db.execute(
                "UPDATE skill_backend_executions SET state='succeeded',output=?,output_sha=?,error=NULL,finished=? WHERE id=? AND task_id=? AND state='unknown' AND calls=1 AND error='gpu_unapproved_future_truth' AND request_sha=?",
                (
                    canonical(output),
                    digest(output),
                    app.store.clock(),
                    execution["id"],
                    TASK,
                    execution["request_sha"],
                ),
            ).rowcount
            == 1
        )
        app.skills.store.event(
            db,
            TASK,
            "operator_recovered_null_truth_postprocessing",
            gpu_receipt=used[0],
            prediction_reexecuted=False,
        )
        assert (
            db.execute(
                "UPDATE model_step_receipts SET state='prepared',error=NULL WHERE scope=? AND run=? AND step_key=? AND state IN ('running','unknown')",
                (row["scope"], row["run_id"], execution["step_key"]),
            ).rowcount
            == 1
        )
    row = app.skills.store.get(row["scope"], TASK)
    app.skills.validate(row)
    context = ExecutionContext(
        row["snapshot"]["run"]["owner_user_id"], row["snapshot"]["run"]["thread_id"]
    )

    async def recover_reports(app, row, worker, generation):
        assert row["task_id"] == TASK
        jobs = app.skills.store
        child = ExecutionContext(context.user_id, "gbar-receipt-recovery-" + uuid4().hex)
        delegation = jobs.delegate(
            row["scope"], TASK, child.scope_key, row["snapshot"]["skills"]["operations"]
        )
        capability = jobs.bind(
            delegation, child.scope_key, "run_" + uuid4().hex, uuid4().hex, worker, generation
        )
        session = jobs.session(capability)

        async def call(name, args):
            return await app.skills.invoke(
                capability, name, args, scope=session["scope"], native_run=session["native_run"]
            )

        sample = await call("scat_sample_prepare", {})
        trial = await call(
            "scat_trial_run",
            {"reference": sample["reference"], "trial_id": sample["plan"]["trial_ids"][0]},
        )
        evaluation = await call("scat_evaluation_summarize", {"references": [trial["reference"]]})
        diagnosis = await call("scat_evaluation_diagnose", {"reference": evaluation["reference"]})
        report = await call(
            "scat_report_build", {"references": [evaluation["reference"], diagnosis["reference"]]}
        )
        return await call("scat_report_read", {"reference": report["reference"]})

    async def pulse():
        while True:
            await asyncio.sleep(5)
            app.skills.store.heartbeat(TASK, worker, generation, lease_seconds=300)

    heartbeat = asyncio.create_task(pulse())
    try:
        await recover_reports(app, row, worker, generation)
        reference = completion_reference(app, row)
        assert reference
        await publish_result(app, row, reference, lease=(worker, generation))
        app.skills.store.state(
            row["scope"],
            TASK,
            "succeeded",
            reference=reference,
            worker=worker,
            generation=generation,
        )
        ensure_account(app.skills.store, row).finish()
        final = await app.get(context, TASK)
        assert final["skill_job"]["state"] == "succeeded", final["skill_job"]["state"]
        assert final["skill_job"]["budget"]["llm_calls"] == budget["llm_calls"]
        print(
            json.dumps(
                {
                    "status": "passed",
                    "task_id": TASK,
                    "gpu_posts_added": 0,
                    "llm_calls_added": 0,
                    "original_gpu_receipt_preserved": used,
                    "approved_snapshot_unchanged": row["sha256"],
                    "final_state": final["skill_job"]["state"],
                    "artifact_count": len(final["run"]["result_artifact_ids"]),
                    "recovery_driver": "trusted_cached_receipt_postprocessing_and_report_steps",
                }
            )
        )
    except BaseException:
        app.skills.store.state(
            row["scope"],
            TASK,
            "unknown",
            error="operator_cached_result_recovery_incomplete",
            worker=worker,
            generation=generation,
        )
        raise
    finally:
        heartbeat.cancel()
        await flight_database.close()


if __name__ == "__main__":
    asyncio.run(main())
