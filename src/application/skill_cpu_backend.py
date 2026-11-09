"""Durable CPU execution IDs; the backend survives loss of its Hermes/worker caller."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

from application.model_step_store import StepError, canonical, digest
from application.skill_job_store import SkillJobStore
from application.task_store import TaskConflict, TaskStore
from scat_model_runtime import RegressionTaskInput, RegressionTaskOutput


def process_start(pid):
    if os.environ.get("SCAT_EXECUTION_REMOTE") == "1":
        from deployment_runtime.client import action_request

        result = action_request(
            Path(__file__).resolve().parents[2],
            os.environ,
            "processes",
            {"action": "birth", "pid": pid},
            timeout=5,
        )
        if result.get("status") != "ok":
            raise TaskConflict("Execution owner unavailable; process state is unknown")
        return result["birth"]
    try:
        # Linux process birth ticks prevent PID reuse from stopping unrelated processes.
        value = Path(f"/proc/{pid}/stat").read_text()
        return value[value.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        return None


class DurableCPUPackage:
    def __init__(self, jobs, *, root, task_id=None, worker=None, generation=None):
        self.jobs, self.root = jobs, Path(root).resolve()
        self.task_id, self.worker, self.generation = task_id, worker, generation

    def start(self):
        if not self.task_id:
            raise StepError("approved_cpu_job_required")
        self.jobs.require_lease(self.task_id, self.worker, self.generation)

    def stop(self):
        pass  # Backend lifetime is owned by its execution receipt, not a client close callback.

    def run(self, *, model_input, parameters):
        if parameters:
            raise StepError("unsupported_cpu_parameters")
        request = {"input": model_input.model_dump(mode="json"), "parameters": dict(parameters)}
        request_sha = digest(request)
        # One approved CPU experiment per S4 job. S5 adds explicit repeat/seed identities.
        identity = digest({"job": self.task_id, "operation": "scat_model_run"})
        with self.jobs.transaction() as db:
            self.jobs.fence(db, self.task_id, self.worker, self.generation)
            step = db.execute(
                "SELECT step_key FROM model_step_receipts WHERE scope=(SELECT scope FROM model_tasks WHERE task_id=?) AND run=(SELECT run_id FROM model_tasks WHERE task_id=?) AND operation='scat_model_run' AND state='running'",
                (self.task_id, self.task_id),
            ).fetchall()
            if len(step) != 1:
                raise StepError("cpu_step_receipt_required")
            old = db.execute(
                "SELECT * FROM skill_backend_executions WHERE id=?", (identity,)
            ).fetchone()
            if old is not None and old["request_sha"] != request_sha:
                raise StepError("backend_execution_identity_conflict")
            if old is None:
                db.execute(
                    "INSERT INTO skill_backend_executions(id,task_id,step_key,generation,worker,state,request,request_sha) VALUES(?,?,?,?,?,'prepared',?,?)",
                    (
                        identity,
                        self.task_id,
                        step[0][0],
                        self.generation,
                        self.worker,
                        canonical(request),
                        request_sha,
                    ),
                )
            elif old["state"] == "prepared":
                db.execute(
                    "UPDATE skill_backend_executions SET generation=?,worker=? WHERE id=? AND state='prepared'",
                    (self.generation, self.worker, identity),
                )
            self.jobs.event(
                db, self.task_id, "backend_bound", execution_id=identity, request_sha256=request_sha
            )
        current = inspect_execution(self.jobs, identity)
        if current["state"] == "prepared":
            log_path = self.root / "var/validation" / ("skill-cpu-" + identity + ".log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                log_path.chmod(0o600)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        str(self.root / "scripts/run_skill_cpu_backend.py"),
                        "--state",
                        str(self.jobs.path),
                        "--execution",
                        identity,
                        "--generation",
                        str(self.generation),
                    ],
                    cwd=self.root,
                    env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=stream,
                    start_new_session=True,
                )
            with self.jobs.transaction() as db:
                db.execute(
                    "UPDATE skill_backend_executions SET pid=?,process_start=? WHERE id=? AND generation=? AND state='prepared'",
                    (child.pid, process_start(child.pid), identity, self.generation),
                )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = inspect_execution(self.jobs, identity)
            if current["state"] == "succeeded":
                output = json.loads(current["output"])
                if digest(output) != current["output_sha"]:
                    raise StepError("backend_output_integrity_failed")
                return RegressionTaskOutput.model_validate(output)
            if current["state"] in {"failed", "cancelled", "unknown"}:
                raise StepError("backend_" + current["state"])
            time.sleep(0.05)
        raise StepError("backend_outcome_unknown")


def inspect_execution(jobs, identity):
    with closing(jobs.tasks.connect()) as db:
        row = db.execute(
            "SELECT * FROM skill_backend_executions WHERE id=?", (identity,)
        ).fetchone()
    return dict(row) if row else None


def request_stop(jobs, task_id):
    if os.environ.get("SCAT_EXECUTION_REMOTE") == "1":
        from deployment_runtime.client import action_request

        result = action_request(
            Path(__file__).resolve().parents[2],
            os.environ,
            "processes",
            {"action": "stop", "task_id": task_id},
            timeout=5,
        )
        if result.get("status") != "ok":
            raise TaskConflict("Execution owner unavailable; cancellation is unknown")
        return
    with closing(jobs.tasks.connect()) as db:
        rows = db.execute(
            "SELECT * FROM skill_backend_executions WHERE task_id=? AND state='running'", (task_id,)
        ).fetchall()
    for row in rows:
        if (
            row["pid"]
            and row["process_start"]
            and process_start(row["pid"]) == row["process_start"]
        ):
            try:
                os.kill(row["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


def execute_backend(root, state_path, identity, generation):
    """Only this process transitions prepared->running and produces an external receipt."""
    from scat_model_runtime.cpu_fixture import SyntheticCV

    jobs = SkillJobStore(TaskStore(Path(state_path), workspace_root=Path(root)))
    with jobs.transaction() as db:
        row = db.execute(
            "SELECT * FROM skill_backend_executions WHERE id=?", (identity,)
        ).fetchone()
        if row is None or row["state"] != "prepared" or row["generation"] != generation:
            return
        jobs.fence(db, row["task_id"], row["worker"], generation)
        task = jobs.tasks._decode(
            db.execute("SELECT * FROM model_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        )
        if task["snapshot"]["request"]["model_key"] != "synthetic-cpu-cv":
            raise TaskConflict("Unreviewed CPU model")
        request = json.loads(row["request"])
        if digest(request) != row["request_sha"]:
            raise TaskConflict("Backend input integrity failed")
        if digest(request["input"]) != task["snapshot"]["skills"]["input_sha256"]:
            raise TaskConflict("Backend input differs from approval")
        budget = db.execute(
            "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
            (task["scope"], task["run_id"]),
        ).fetchone()
        if (
            budget is None
            or budget["exhausted"]
            or jobs.clock() - budget["started_at"]
            >= json.loads(budget["limits_json"])["wall_seconds"]
        ):
            raise TaskConflict("CPU job budget exhausted")
        db.execute(
            "UPDATE skill_backend_executions SET state='running',pid=?,process_start=?,started=?,calls=calls+1 WHERE id=? AND state='prepared'",
            (os.getpid(), process_start(os.getpid()), jobs.clock(), identity),
        )
        jobs.event(db, row["task_id"], "backend_started", execution_id=identity)
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    try:
        # A deployment-only delay enables actual process fault tests; never a tool parameter.
        delay = float(os.environ.get("SCAT_S4_CPU_DELAY_SECONDS", "0"))
        if not 0 <= delay <= 30:
            raise ValueError("Invalid CPU test delay")
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not stopped:
            time.sleep(0.05)
        if stopped:
            status, output, error = "cancelled", None, "cpu_stop_acknowledged"
        else:
            model_input = RegressionTaskInput.model_validate(request["input"])
            output = (
                SyntheticCV()
                .run(model_input=model_input, parameters=request["parameters"])
                .model_dump(mode="json")
            )
            status, error = "succeeded", None
    except Exception:  # noqa: BLE001 -- backend receipt contains a fixed error only
        status, output, error = "failed", None, "cpu_execution_failed"
    with jobs.transaction() as db:
        # Completion of an already admitted calculation remains evidence after lease loss.
        db.execute(
            "UPDATE skill_backend_executions SET state=?,output=?,output_sha=?,error=?,finished=? WHERE id=? AND state='running'",
            (
                status,
                canonical(output) if output else None,
                digest(output) if output else None,
                error,
                jobs.clock(),
                identity,
            ),
        )
        jobs.event(db, row["task_id"], "backend_terminal", execution_id=identity, state=status)
