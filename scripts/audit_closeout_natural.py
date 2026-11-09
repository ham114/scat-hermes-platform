"""Verify one ordinary-language GPU task, its frozen input and unchanged old receipts."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"
sys.path.insert(0, str(ROOT / "src"))


def main():
    from application.task_store import digest

    record = json.loads((OUT / "natural-acceptance.json").read_text())
    assert record["status"] == "passed" and record["submissions"] == record["confirmations"] == 1
    assert record["maintenance_recovery_used"] is False
    before = json.loads((OUT / "audit-before.json").read_text())
    taskdb = ROOT / "var/deploy/h7b/shared/state/tasks.sqlite"
    with sqlite3.connect("file:" + str(taskdb) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        task = db.execute(
            "SELECT * FROM model_tasks WHERE task_id=?", (record["task_id"],)
        ).fetchone()
        snapshot = json.loads(task["snapshot"])
        assert digest(snapshot) == task["sha256"] == record["approved_snapshot_sha256"]
        assert snapshot["skills"]["parent_budget"] is not None
        job = db.execute(
            "SELECT * FROM skill_jobs WHERE task_id=?", (record["task_id"],)
        ).fetchone()
        assert (
            job["state"] == "succeeded"
            and job["parent_imported"] == 1
            and job["budget_revision"] == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM skill_job_sessions WHERE task_id=?", (record["task_id"],)
            ).fetchone()[0]
            == 1
        )
        events = db.execute(
            "SELECT kind,data FROM skill_job_events WHERE task_id=?", (record["task_id"],)
        ).fetchall()
        assert sum(x["kind"] == "native_run_bound" for x in events) == 1
        assert not any(
            x["kind"].startswith("operator_") or x["kind"] in {"paused", "unknown", "lease_expired"}
            for x in events
        )
        required = {
            "scat_sample_prepare",
            "scat_trial_run",
            "scat_evaluation_summarize",
            "scat_evaluation_diagnose",
            "scat_report_build",
            "scat_report_read",
        }
        returned = {
            json.loads(e["data"]).get("operation") for e in events if e["kind"] == "step_returned"
        }
        assert required <= returned
        execution = db.execute(
            "SELECT * FROM skill_backend_executions WHERE task_id=?", (record["task_id"],)
        ).fetchall()
        assert (
            len(execution) == 1
            and execution[0]["calls"] == 1
            and execution[0]["state"] == "succeeded"
        )
        output = json.loads(execution[0]["output"])
        assert (
            digest(output) == execution[0]["output_sha"]
            and not output["samples"]
            and output["prediction_count"] == 3
        )
        budget = dict(
            db.execute(
                "SELECT * FROM skill_run_budgets WHERE run_id=?", (task["run_id"],)
            ).fetchone()
        )
        assert (
            not budget["exhausted"]
            and budget["charged_tokens"] <= 1000000
            and budget["llm_calls"] <= 24
        )
        for row in db.execute(
            "SELECT body,sha FROM model_step_artifacts WHERE scope=? AND run=?",
            (task["scope"], task["run_id"]),
        ):
            assert digest(json.loads(row["body"])) == row["sha"]
    container = json.loads(
        subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"])
    )[0]
    inputs = Path(
        next(
            m["Source"]
            for m in container["Mounts"]
            if m["Destination"] == str(ROOT / "var/workflow-inputs")
        )
    )
    frozen = json.loads((inputs / (snapshot["workflow"]["input_sha256"] + ".json")).read_text())
    assert digest(frozen) == snapshot["workflow"]["input_sha256"] and not frozen["synthetic"]
    flight = frozen["flights"][0]
    window = flight["gbar_windows"][0]
    assert len(frozen["flights"]) == len(flight["gbar_windows"]) == 1
    assert len(flight["data"]["plots"]) == len(window["timestamps"]) == 200
    assert (
        window["annotations"]["model_resampled_points"] == 36
        and not window["annotations"]["actual_future_candidates"]
    )
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        db.row_factory = sqlite3.Row
        receipts = [
            dict(r)
            for r in db.execute(
                "SELECT id,task,execution,state,calls,request_sha,output_sha FROM gpu_requests ORDER BY created"
            )
        ]
    old = {r["id"]: r for r in before["gpu_receipts"]}
    new = {r["id"]: r for r in receipts}
    added = set(new) - set(old)
    assert len(added) == 1 and all(new[k] == v for k, v in old.items())
    assert (
        new[next(iter(added))]["task"] == record["task_id"] and new[next(iter(added))]["calls"] == 1
    )
    report = {
        "status": "passed",
        "thread_id": record["thread_id"],
        "task_id": record["task_id"],
        "planning_run_id": record["planning_run_id"],
        "ordinary_natural_language_submissions": 1,
        "page_confirmations": 1,
        "actual_gpu_posts": 1,
        "native_worker_sessions": 1,
        "maintenance_recovery_used": False,
        "raw_points": 200,
        "model_steps": 36,
        "future_predictions": 3,
        "error_samples_without_truth": 0,
        "prior_gpu_receipts_unchanged": len(old),
        "phases_returned": sorted(required),
        "budget_includes_parent_planning": True,
        "usage": {
            k: budget[k]
            for k in [
                "llm_calls",
                "tool_calls",
                "charged_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
            ]
        },
    }
    (OUT / "natural-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
