"""S5 independent numerical executions using the S4 durable receipt and lease tables."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

from application.model_step_store import StepError
from application.skill_cpu_backend import inspect_execution, process_start
from application.skill_job_store import SkillJobStore
from application.task_store import TaskConflict, TaskStore, canonical, digest
from workflow_runtime.data import load_input


def run(jobs, root, row, session, step_key, operation, unit):
    request = {
        "schema_version": "workflow-execution/v1",
        "operation": operation,
        "unit": unit,
        "input_sha256": row["snapshot"]["workflow"]["input_sha256"],
        "plan_sha256": row["snapshot"]["workflow"]["plan_sha256"],
    }
    identity = digest({"job": row["task_id"], "operation": operation, "unit": unit})
    with jobs.transaction() as db:
        jobs.fence(
            db, row["task_id"], session["worker"], session["generation"], session["revision"]
        )
        old = db.execute(
            "SELECT * FROM skill_backend_executions WHERE id=?", (identity,)
        ).fetchone()
        if old is not None and old["request_sha"] != digest(request):
            raise StepError("workflow_backend_identity_conflict")
        if old is None or old["state"] == "prepared":
            if operation == "scat_materialize" and unit == "prediction_metric_summaries":
                ready = {
                    json.loads(r[0])["unit"]
                    for r in db.execute(
                        "SELECT request FROM skill_backend_executions WHERE task_id=? AND state='succeeded'",
                        (row["task_id"],),
                    )
                    if json.loads(r[0])["operation"] == "scat_materialize"
                }
                if not {"prediction_metrics", "phase_segments", "flight_features"} <= ready:
                    raise StepError("workflow_prerequisites_incomplete")
            if db.execute(
                "SELECT 1 FROM skill_backend_executions WHERE task_id=? AND id<>? AND state IN ('prepared','running','unknown')",
                (row["task_id"], identity),
            ).fetchone():
                raise StepError("workflow_resource_busy")
            if row["snapshot"]["workflow"]["proposal"]["failure_policy"] == "stop":
                completed = db.execute(
                    "SELECT output FROM skill_backend_executions WHERE task_id=? AND state='succeeded'",
                    (row["task_id"],),
                ).fetchall()
                if any(json.loads(v[0]).get("status") != "succeeded" for v in completed):
                    raise StepError("workflow_failure_policy_stopped")
        if old is None:
            db.execute(
                "INSERT INTO skill_backend_executions(id,task_id,step_key,generation,worker,state,request,request_sha) VALUES(?,?,?,?,?,'prepared',?,?)",
                (
                    identity,
                    row["task_id"],
                    step_key,
                    session["generation"],
                    session["worker"],
                    canonical(request),
                    digest(request),
                ),
            )
        elif old["state"] == "prepared":
            db.execute(
                "UPDATE skill_backend_executions SET generation=?,worker=? WHERE id=? AND state='prepared'",
                (session["generation"], session["worker"], identity),
            )
        jobs.event(
            db,
            row["task_id"],
            "workflow_backend_bound",
            execution_id=identity,
            operation=operation,
            unit=unit,
        )
    if inspect_execution(jobs, identity)["state"] == "prepared":
        log_path = Path(root) / "var/validation" / ("workflow-backend-" + identity + ".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            log_path.chmod(0o600)
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(root) / "scripts/run_workflow_backend.py"),
                    "--state",
                    str(jobs.path),
                    "--execution",
                    identity,
                    "--generation",
                    str(session["generation"]),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
                env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
            )
        with jobs.transaction() as db:
            db.execute(
                "UPDATE skill_backend_executions SET pid=?,process_start=? WHERE id=? AND generation=? AND state='prepared'",
                (child.pid, process_start(child.pid), identity, session["generation"]),
            )
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        current = inspect_execution(jobs, identity)
        if current["state"] == "succeeded":
            output = json.loads(current["output"])
            if digest(output) != current["output_sha"]:
                raise StepError("workflow_backend_output_integrity")
            return identity, output
        if current["state"] in {"unknown", "failed", "cancelled"}:
            raise StepError("workflow_backend_" + current["state"])
        time.sleep(0.05)
    raise StepError("workflow_backend_outcome_unknown")


def execute(root, state, identity, generation):
    from hermes_runtime.job_context import load_approved_job
    from scat_model_runtime.backends.base import BackendError
    from workflow_runtime.numerics import materialize, run_trial

    root = Path(root).resolve()
    jobs = SkillJobStore(TaskStore(Path(state), workspace_root=root))
    with closing(jobs.tasks.connect()) as db:
        raw = db.execute(
            "SELECT * FROM skill_backend_executions WHERE id=?", (identity,)
        ).fetchone()
    if raw is None or raw["state"] != "prepared" or raw["generation"] != generation:
        return
    _, row = load_approved_job(root, state, raw["task_id"], raw["worker"], generation)
    plan = row["snapshot"]["workflow"]
    request = json.loads(raw["request"])
    if (
        digest(request) != raw["request_sha"]
        or request["input_sha256"] != plan["input_sha256"]
        or request["plan_sha256"] != plan["plan_sha256"]
    ):
        raise TaskConflict("Backend approval mismatch")
    unit, operation = request["unit"], request["operation"]
    if operation not in {"scat_trial_run", "scat_materialize"}:
        raise TaskConflict("Unsupported independent workflow effect")
    if operation not in row["snapshot"]["skills"]["operations"]:
        raise TaskConflict("Backend operation outside approval")
    trial = next((t for t in plan["trials"] if t["trial_id"] == unit), None)
    if (operation == "scat_trial_run" and trial is None) or (
        operation == "scat_materialize" and unit not in plan["proposal"]["materializations"]
    ):
        raise TaskConflict("Backend unit outside approval")
    frozen = load_input(root, plan["input_sha256"], scope=row["scope"])
    prerequisites = {}
    if operation == "scat_materialize" and unit == "prediction_metric_summaries":
        with closing(jobs.tasks.connect()) as db:
            records = db.execute(
                "SELECT * FROM skill_backend_executions WHERE task_id=? AND state='succeeded'",
                (row["task_id"],),
            ).fetchall()
        for r in records:
            request_body = json.loads(r["request"])
            if request_body["operation"] == "scat_materialize":
                result = json.loads(r["output"])
                if (
                    digest(result) != r["output_sha"]
                    or request_body["input_sha256"] != plan["input_sha256"]
                ):
                    raise TaskConflict("Prerequisite receipt integrity failed")
                prerequisites[request_body["unit"]] = result
        if not {"prediction_metrics", "phase_segments", "flight_features"} <= prerequisites.keys():
            raise TaskConflict("Materialization prerequisites are not complete")
    with jobs.transaction() as db:
        jobs.fence(db, row["task_id"], raw["worker"], generation)
        current = db.execute(
            "SELECT state,generation FROM skill_backend_executions WHERE id=?", (identity,)
        ).fetchone()
        if current["state"] != "prepared" or current["generation"] != generation:
            return
        budget = db.execute(
            "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
            (row["scope"], row["run_id"]),
        ).fetchone()
        if (
            budget is None
            or budget["exhausted"]
            or jobs.clock() - budget["started_at"]
            >= json.loads(budget["limits_json"])["wall_seconds"]
        ):
            raise TaskConflict("Workflow budget exhausted")
        db.execute(
            "UPDATE skill_backend_executions SET state='running',pid=?,process_start=?,started=?,calls=calls+1 WHERE id=? AND state='prepared'",
            (os.getpid(), process_start(os.getpid()), jobs.clock(), identity),
        )
        jobs.event(db, row["task_id"], "workflow_backend_started", execution_id=identity, unit=unit)
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    output, error = None, None
    try:
        delay = float(os.environ.get("SCAT_S5_CPU_DELAY_SECONDS", "0"))
        if not 0 <= delay <= 30:
            raise ValueError("Invalid deployment test delay")
        until = time.monotonic() + delay
        while time.monotonic() < until and not stopped:
            time.sleep(0.05)
        if stopped:
            status, error = "cancelled", "workflow_stop_acknowledged"
        else:
            if operation == "scat_trial_run" and trial["configuration"]["model"] == "scat-gbar-v23":
                from workflow_runtime.gpu import run_gpu_trial

                def authorize_gpu():
                    jobs.require_lease(row["task_id"], raw["worker"], generation)
                    if stopped:
                        raise BackendError("gpu_cancelled_before_send")
                    from workflow_runtime.gpu import registration

                    registration(root, required=True, expected=plan["gpu"])
                    with closing(jobs.tasks.connect()) as account_db:
                        account = account_db.execute(
                            "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                            (row["scope"], row["run_id"]),
                        ).fetchone()
                    if (
                        account is None
                        or account["exhausted"]
                        or account["finished_at"] is not None
                        or jobs.clock() - account["started_at"]
                        >= json.loads(account["limits_json"])["wall_seconds"]
                    ):
                        raise BackendError("gpu_job_budget_exhausted")

                output = run_gpu_trial(
                    root,
                    row,
                    identity,
                    trial,
                    frozen,
                    authorize=authorize_gpu,
                    stopped=lambda: stopped,
                )
            elif operation == "scat_trial_run":
                output = run_trial(frozen, plan["proposal"], trial)
            else:
                output = materialize(
                    frozen,
                    unit,
                    generated_at=row["snapshot"]["run"]["created_at"],
                    proposal=plan["proposal"],
                    prerequisites=prerequisites,
                )
            status = "cancelled" if stopped else "succeeded"
    except BackendError as exc:
        status = "unknown" if exc.execution_state != "not_started" else "failed"
        error = exc.code
    except Exception:  # noqa: BLE001 -- fixed durable boundary error, raw data stays private
        status, error = "failed", "workflow_numerical_execution_failed"
    with jobs.transaction() as db:
        db.execute(
            "UPDATE skill_backend_executions SET state=?,output=?,output_sha=?,error=?,finished=? WHERE id=? AND state='running'",
            (
                status,
                canonical(output) if output is not None else None,
                digest(output) if output is not None else None,
                error,
                jobs.clock(),
                identity,
            ),
        )
        jobs.event(
            db, row["task_id"], "workflow_backend_terminal", execution_id=identity, state=status
        )
