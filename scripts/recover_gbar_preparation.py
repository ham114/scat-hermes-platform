"""Recover one rejected, zero-effect preparation using its original audited tool payload."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
sys.path.insert(0, str(ROOT / "src"))
from application.task_store import digest


def main():
    file = OUT / "forecast.json"
    record = json.loads(file.read_text())
    assert (
        record["confirmations"] == 0
        and "task_id" not in record
        and not record.get("recovery_attempts")
    )
    run = record["planning_run_id"]
    home = ROOT / "var/deploy/h7b/shared/homes/main"
    with sqlite3.connect(
        "file:" + str(home / "skill-loads.sqlite") + "?mode=ro", uri=True
    ) as trace:
        trace.row_factory = sqlite3.Row
        operations = trace.execute(
            "SELECT * FROM skill_operations WHERE run_id=? AND operation='scat_workflow_prepare'",
            (run,),
        ).fetchall()
        assert len(operations) == 1
        operation = operations[0]
        assert (
            operation["skill_id"] == "scat-model-comparison"
            and operation["skill_version"] == "0.3.0"
        )
        budget = dict(
            trace.execute("SELECT * FROM skill_run_budgets WHERE run_id=?", (run,)).fetchone()
        )
        assert budget["finished_at"] is not None and not budget["exhausted"]
        assert budget["llm_calls"] < 24 and budget["charged_tokens"] < 1000000
    arguments = []
    with sqlite3.connect("file:" + str(home / "state.db") + "?mode=ro", uri=True) as native:
        for (body,) in native.execute(
            "SELECT tool_calls FROM messages WHERE session_id=? AND tool_calls IS NOT NULL",
            (operation["session_id"],),
        ):
            for call in json.loads(body):
                function = call.get("function", call)
                if function.get("name") == "scat_workflow_prepare":
                    value = function["arguments"]
                    args = json.loads(value) if isinstance(value, str) else value
                    if digest(args) == operation["arguments_sha256"]:
                        arguments.append(args)
    assert arguments and all(a == arguments[0] for a in arguments)
    args = arguments[0]
    p = args["proposal"]
    assert (
        p["kind"] == "forecast"
        and p["observation_last_n"] == 200
        and p["external_ids"] == ["100002"]
    )
    assert (
        p["horizons_s"] == [10, 30, 60]
        and len(p["models"]) == 1
        and p["models"][0]["model"] == "scat-gbar-v23"
    )
    with sqlite3.connect(
        "file:" + str(ROOT / "var/deploy/h7b/shared/state/tasks.sqlite") + "?mode=ro", uri=True
    ) as tasks:
        assert (
            tasks.execute(
                "SELECT count(*) FROM model_tasks WHERE scope=?", (operation["scope"],)
            ).fetchone()[0]
            == 0
        )
    payload = {
        "operation": "scat_workflow_prepare",
        "arguments": args,
        "scope": operation["scope"],
        "source_run_id": run,
        "proposal_key": run + ":" + operation["arguments_sha256"],
    }
    record.update(
        initial_planning_status="completed_without_task",
        initial_preparation_error="RuntimeError: cold MongoDB connection before index preflight",
        recovery_attempts=1,
        recovery_payload_sha256=digest(payload),
        status="original_preparation_recovery_send_intent",
    )
    file.write_text(json.dumps(record, indent=2) + "\n")
    code = "import sys,json,asyncio;sys.path.insert(0,'scripts');from run_workflow_tool import invoke;print(json.dumps(asyncio.run(invoke(json.load(sys.stdin)))))"
    response = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "scat-hermes-h7b-actions-1",
            "/opt/conda/envs/scat-agent/bin/python",
            "-B",
            "-c",
            code,
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    result = json.loads(response.stdout)
    assert result["status"] == "awaiting_confirmation"
    task = result["data"]
    assert task["state"] == "pending" and task["skill_job"]["includes_parent_planning_usage"]
    record.update(
        task_id=task["task_id"],
        execution_run_id=task["run_id"],
        status="pending",
        preparation_recovered_without_new_llm_or_gpu=True,
    )
    file.write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "recovered_pending_confirmation",
                "task_id": task["task_id"],
                "parent_usage_retained": True,
                "new_llm": 0,
                "gpu_posts": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
