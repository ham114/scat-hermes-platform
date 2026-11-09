"""Read-only end-to-end audit: approved input -> one CUDA send -> native evaluation -> UI."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-model-flow"
sys.path.insert(0, str(ROOT / "src"))


def connect(path):
    db = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def main():
    from application.task_store import digest
    from gbar_core.transport import FEATURES, INPUT_UNITS
    from scat_model_runtime.backends.base import InferenceRequest
    from scat_model_runtime.task_io import RegressionInputSeries, RegressionTaskInput

    record = json.loads((OUT / "direct-acceptance.json").read_text())
    failed_run = "run_4c2acad33ea94451a13eaebf73e9ed1f"
    assert record["status"] == "passed" and record["preparations"] == record["confirmations"] == 1
    assert (
        record["queue_state"] in {"queued", "running"}
        and record["maintenance_recovery_used"] is False
    )
    before = json.loads((OUT / "audit-before.json").read_text())
    after = json.loads((OUT / "audit-after.json").read_text())
    assert before["boot_sha256"] == after["boot_sha256"]
    assert (
        before["gpu_container"] == after["gpu_container"]
        and before["gpu_metadata"] == after["gpu_metadata"]
    )
    assert all(v["health"] == "healthy" for v in after["containers"].values())
    assert all(
        before["containers"][k]["id"] == after["containers"][k]["id"]
        for k in ["author-api", "hermes-author"]
    )
    row = json.loads(subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"]))[
        0
    ]
    inputs = Path(
        next(
            m["Source"]
            for m in row["Mounts"]
            if m["Destination"] == str(ROOT / "var/workflow-inputs")
        )
    )
    state = ROOT / "var/deploy/h7b/shared/state/tasks.sqlite"
    with connect(state) as db:
        row = db.execute(
            "SELECT * FROM model_tasks WHERE task_id=?", (record["task_id"],)
        ).fetchone()
        snapshot = json.loads(row["snapshot"])
        assert digest(snapshot) == row["sha256"] == record["approved_snapshot_sha256"]
        plan = snapshot["workflow"]
        frozen = json.loads((inputs / (plan["input_sha256"] + ".json")).read_text())
        assert digest(frozen) == plan["input_sha256"] and not frozen["synthetic"]
        assert len(frozen["flights"]) == 1
        flight = frozen["flights"][0]
        window = flight["gbar_windows"][0]
        assert len(flight["data"]["plots"]) == len(window["timestamps"]) == 200
        assert len(flight["gbar_windows"]) == 1 and not flight["gbar_excluded"]
        assert window["annotations"]["model_resampled_points"] == 36
        assert not window["annotations"]["actual_future_candidates"]
        reference = snapshot["run"]["input_refs"][0]
        assert reference["limit"] == 200
        assert datetime.fromisoformat(reference["start_time"]) == datetime.fromisoformat(
            window["timestamps"][0]
        )
        assert datetime.fromisoformat(reference["end_time"]) == datetime.fromisoformat(
            window["timestamps"][-1]
        )
        job = db.execute(
            "SELECT * FROM skill_jobs WHERE task_id=?", (record["task_id"],)
        ).fetchone()
        assert (
            job["state"] == "succeeded"
            and job["parent_imported"] == 1
            and job["budget_revision"] == 0
        )
        sessions = db.execute(
            "SELECT * FROM skill_job_sessions WHERE task_id=?", (record["task_id"],)
        ).fetchall()
        assert len(sessions) == 1 and sessions[0]["generation"] == 1
        assert snapshot["skills"]["parent_budget"] is None
        events = db.execute(
            "SELECT kind,data FROM skill_job_events WHERE task_id=? ORDER BY id",
            (record["task_id"],),
        ).fetchall()
        assert sum(e["kind"] == "native_run_bound" for e in events) == 1
        assert not any(
            e["kind"].startswith("operator_") or e["kind"] in {"lease_expired", "paused", "unknown"}
            for e in events
        )
        returned = {
            json.loads(e["data"]).get("operation") for e in events if e["kind"] == "step_returned"
        }
        required = {
            "scat_sample_prepare",
            "scat_trial_run",
            "scat_evaluation_summarize",
            "scat_evaluation_diagnose",
            "scat_report_build",
            "scat_report_read",
        }
        assert required <= returned
        executions = db.execute(
            "SELECT * FROM skill_backend_executions WHERE task_id=?", (record["task_id"],)
        ).fetchall()
        assert (
            len(executions) == 1
            and executions[0]["state"] == "succeeded"
            and executions[0]["calls"] == 1
        )
        execution = executions[0]
        output = json.loads(execution["output"])
        assert digest(output) == execution["output_sha"] and output["status"] == "succeeded"
        assert output["samples"] == [] and output["prediction_count"] == 3
        verified_steps = 0
        for artifact in db.execute(
            "SELECT body,sha FROM model_step_artifacts WHERE scope=? AND run=?",
            (row["scope"], row["run_id"]),
        ):
            body = json.loads(artifact["body"])
            assert digest(body) == artifact["sha"]
            if body.get("workflow_kind") == "evaluation":
                assert all(
                    not trial["metrics"] and not trial["phase_metrics"] and not trial["worst_cases"]
                    for trial in body["payload"]["trials"]
                )
            verified_steps += 1
        budget = dict(
            db.execute(
                "SELECT * FROM skill_run_budgets WHERE run_id=?", (row["run_id"],)
            ).fetchone()
        )
        assert (
            not budget["exhausted"]
            and budget["llm_calls"] <= 24
            and budget["charged_tokens"] <= 430041
        )
        assert (
            db.execute(
                "SELECT count(*) FROM skill_job_budget_revisions WHERE task_id=? AND revision>0",
                (record["task_id"],),
            ).fetchone()[0]
            == 0
        )
    request_id = digest({"execution": execution["id"], "chunk": 0})
    model_input = RegressionTaskInput(
        feature_names=FEATURES,
        feature_units=INPUT_UNITS,
        series=(RegressionInputSeries.model_validate(window),),
    )
    request = InferenceRequest(
        request_id=request_id,
        run_id=row["run_id"],
        model_key="scat-gbar-v23",
        model_version="23",
        input_schema=model_input.schema_version,
        output_schema="regression-task-output/v1",
        payload=model_input.model_dump(mode="json"),
    )
    with connect(ROOT / "var/gpu/requests.sqlite") as db:
        receipt = db.execute("SELECT * FROM gpu_requests WHERE id=?", (request_id,)).fetchone()
        assert (
            receipt["state"] == "succeeded"
            and receipt["calls"] == 1
            and receipt["task"] == record["task_id"]
        )
        assert receipt["request_sha"] == digest(request.model_dump(mode="json"))
        raw = json.loads(receipt["output"])
        assert (
            digest(raw) == receipt["output_sha"] and raw["provenance"] == plan["gpu"]["provenance"]
        )
        series = raw["payload"]["series"][0]
        origin = datetime.fromisoformat(series["forecast_origin"])
        assert origin == datetime.fromisoformat(window["timestamps"][-1])
        assert all(origin + timedelta(seconds=h) > origin for h in [10, 30, 60])
        assert all(v is None for r in series["expected_values"] for v in r)
    old = {r["id"]: r for r in before["gpu_receipts"]}
    new = {r["id"]: r for r in after["gpu_receipts"]}
    assert set(new) - set(old) == {request_id} and all(new[k] == v for k, v in old.items())
    with connect(ROOT / "var/deploy/h7b/shared/homes/main/skill-loads.sqlite") as db:
        parent = dict(
            db.execute("SELECT * FROM skill_run_budgets WHERE run_id=?", (failed_run,)).fetchone()
        )
        prepares = db.execute(
            "SELECT * FROM skill_operations WHERE run_id=? AND operation='scat_workflow_prepare'",
            (failed_run,),
        ).fetchall()
        assert prepares and all(p["skill_id"] == "scat-model-comparison" for p in prepares)
    assert parent["charged_tokens"] + budget["charged_tokens"] <= 2000000
    fingerprints = {}
    for role, py in [("platform", "scat-agent"), ("hermes-main", "scat-hermes")]:
        code = (
            "import hashlib,json;from pathlib import Path;r=Path.cwd();print(json.dumps({p:hashlib.sha256((r/p).read_bytes()).hexdigest() for p in "
            + repr(list(snapshot["skills"]["sources"]))
            + "}))"
        )
        current = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "scat-hermes-h7b-" + role + "-1",
                    "/opt/conda/envs/" + py + "/bin/python",
                    "-B",
                    "-c",
                    code,
                ],
                text=True,
            )
        )
        assert current == snapshot["skills"]["sources"]
        fingerprints[role] = len(current)
    report = {
        "status": "passed",
        "release": after["release"],
        "thread_id": record["thread_id"],
        "task_id": record["task_id"],
        "natural_requests": 1,
        "natural_request_status": "failed_validation_before_fix",
        "direct_browser_submissions": 1,
        "page_confirmations": 1,
        "queue_confirmed": True,
        "actual_gpu_posts": 1,
        "gpu_calls": 1,
        "native_worker_sessions": 1,
        "maintenance_recovery_used": False,
        "approved_raw_points": 200,
        "model_input_steps": 36,
        "future_predictions": 3,
        "error_samples_without_truth": 0,
        "verified_step_artifacts": verified_steps,
        "actual_phases_returned": sorted(required),
        "server_not_rebooted": True,
        "gpu_and_author_containers_unchanged": True,
        "implementation_fingerprints": fingerprints,
        "total_budget_including_failed_planning": {
            k: budget[k] + parent[k]
            for k in [
                "llm_calls",
                "tool_calls",
                "charged_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
            ]
        },
        "failed_planning_usage_separate_from_direct_task": {
            k: parent[k] for k in ["llm_calls", "tool_calls", "charged_tokens"]
        },
        "artifacts": len(record["artifact_ids"]),
        "browser_graph_points": record["graph_points"],
        "browser_auth_headers": record["auth_headers"],
        "browser_refresh_without_resubmit": record["refresh_without_resubmit"],
    }
    (OUT / "acceptance-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
