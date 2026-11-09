"""Read-only provenance/usage audit; retain metadata, never raw trajectory or conversation."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-forecast-contract"


def database(path):
    db = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def main():
    sys.path.insert(0, str(ROOT / "src"))
    from application.task_store import digest
    from workflow_runtime.contracts import WorkflowProposal
    from workflow_runtime.windows import verified_windows

    record = json.loads((OUT / "natural-acceptance.json").read_text())
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    execution_plan = json.loads((OUT / "execution-baseline-deployment-plan.json").read_text())
    current = ROOT / "var/deploy/h7b/shared/state/tasks.sqlite"
    backup = OUT / "state-before-switch" / execution_plan["release"] / current.relative_to(ROOT)
    with database(backup) as db:
        old_tasks = {r[0] for r in db.execute("SELECT task_id FROM model_tasks")}
    with database(current) as db:
        new_tasks = {r[0] for r in db.execute("SELECT task_id FROM model_tasks")} - old_tasks
        assert new_tasks == {record["task_id"]}
        row = db.execute(
            "SELECT * FROM model_tasks WHERE task_id=?", (record["task_id"],)
        ).fetchone()
        snapshot = json.loads(row["snapshot"])
        assert digest(snapshot) == row["sha256"]
        proposal = WorkflowProposal.model_validate(snapshot["workflow"]["proposal"])
        assert proposal.kind == "forecast" and proposal.observation_last_n == 200
        mount = next(
            item
            for item in json.loads(
                subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"])
            )[0]["Mounts"]
            if item["Destination"] == str(ROOT / "var/workflow-inputs")
        )
        directory = Path(mount["Source"]).resolve()
        assert directory.is_relative_to(ROOT.resolve())
        identity = snapshot["workflow"]["input_sha256"]
        assert len(identity) == 64 and all(c in "0123456789abcdef" for c in identity)
        frozen_path = directory / (identity + ".json")
        assert not frozen_path.is_symlink()
        frozen = json.loads(frozen_path.read_text())
        assert digest(frozen) == identity and frozen["scope"] == row["scope"]
        assert len(frozen["flights"]) == 1
        observed, targets = verified_windows(frozen["flights"][0], proposal)[0]
        assert len(observed) == 200
        reference = snapshot["run"]["input_refs"][0]
        assert reference["limit"] == 200
        assert datetime.fromisoformat(reference["start_time"]) == observed[0].time
        assert datetime.fromisoformat(reference["end_time"]) == observed[-1].time
        job = dict(
            db.execute(
                "SELECT state,parent_imported FROM skill_jobs WHERE task_id=?", (row["task_id"],)
            ).fetchone()
        )
        assert job["state"] == "succeeded" and job["parent_imported"] == 1
        executions = db.execute(
            "SELECT * FROM skill_backend_executions WHERE task_id=?", (row["task_id"],)
        ).fetchall()
        assert (
            len(executions) == 1
            and executions[0]["calls"] == 1
            and executions[0]["state"] == "succeeded"
        )
        output = json.loads(executions[0]["output"])
        assert digest(output) == executions[0]["output_sha"]
        assert (
            output["samples"] == []
            and output["prediction_count"] == 3
            and not output["failed_flights"]
        )
        prediction = output["predictions"][0]
        assert datetime.fromisoformat(prediction["forecast_origin"]) == observed[-1].time
        assert [datetime.fromisoformat(p["timestamp"]) for p in prediction["points"]] == list(
            targets
        )
        assert all(
            target == observed[-1].time + timedelta(seconds=h)
            for target, h in zip(targets, proposal.horizons_s, strict=True)
        )
        artifacts = db.execute(
            "SELECT body,sha FROM model_step_artifacts WHERE scope=? AND run=?",
            (row["scope"], row["run_id"]),
        ).fetchall()
        verified_artifacts = 0
        for item in artifacts:
            body = json.loads(item["body"])
            assert digest(body) == item["sha"]
            if body.get("workflow_kind") == "evaluation":
                assert all(
                    not t["metrics"] and not t["phase_metrics"] and not t["worst_cases"]
                    for t in body["payload"]["trials"]
                )
            if body.get("workflow_kind") == "result":
                assert body["analysis_result"]["context"]["metrics_total_rows"] == 0
            verified_artifacts += 1
        budget = dict(
            db.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (row["run_id"],),
            ).fetchone()
        )
        sessions = [
            dict(r)
            for r in db.execute(
                "SELECT native_run,generation,revision FROM skill_job_sessions WHERE task_id=?",
                (row["task_id"],),
            )
        ]
        assert sessions and all(s["native_run"] != record["planning_run_id"] for s in sessions)
        assert snapshot["skills"]["parent_budget"]["run_id"] == record["planning_run_id"]
    with database(ROOT / "var/deploy/h7b/shared/homes/main/skill-loads.sqlite") as db:
        parent = dict(
            db.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (record["planning_run_id"],),
            ).fetchone()
        )
        failed_record = json.loads((OUT / "natural-first-failed.json").read_text())
        failed_parent = dict(
            db.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (failed_record["planning_run_id"],),
            ).fetchone()
        )
    report = {
        "status": "passed",
        "release": plan["release"],
        "new_natural_requests": 2,
        "failed_planning_only_request": {
            "run_id": failed_record["planning_run_id"],
            "task_created": False,
            "prediction_calls": 0,
            "budget": failed_parent,
        },
        "new_prediction_tasks": len(new_tasks),
        "real_cpu_execution_calls": 1,
        "gpu_calls": 0,
        "exact_observation_points": len(observed),
        "span_s": (observed[-1].time - observed[0].time).total_seconds(),
        "forecast_origin_equals_final_frozen_observation": True,
        "requested_future_horizons_s": list(proposal.horizons_s),
        "target_axis_match": True,
        "error_samples": 0,
        "no_error_metrics_in_evaluation_and_report": True,
        "verified_artifacts": verified_artifacts,
        "independent_native_worker": sessions,
        "parent_budget_bound_and_imported": True,
        "family_budget_including_imported_parent": budget,
        "parent_diagnostic_already_in_family_budget_do_not_add": parent,
        "total_usage_family_plus_failed_planning": {
            key: (budget[key] or 0) + (failed_parent[key] or 0)
            for key in [
                "llm_calls",
                "tool_calls",
                "charged_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
            ]
        },
        "usage_semantics": "charged_tokens are conservative reservation units, not monetary fees or additive to provider tokens",
        "raw_trajectory_or_reply_saved_in_report": False,
    }
    (OUT / "backend-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
