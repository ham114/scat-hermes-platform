"""Verify approved frozen input, native budgets and single-send GPU receipts without execution."""

import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
sys.path.insert(0, str(ROOT / "src"))
from application.task_store import digest


def db(path):
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    before = json.loads((OUT / "audit-before.json").read_text())
    after = json.loads((OUT / "audit-after.json").read_text())
    assert before["boot_sha256"] == after["boot_sha256"]
    assert before["gpu_container"] == after["gpu_container"]
    assert before["gpu_metadata"] == after["gpu_metadata"]
    for role in ["author-api", "hermes-author"]:
        assert before["containers"][role]["id"] == after["containers"][role]["id"]
    assert all(c["health"] == "healthy" for c in after["containers"].values())
    assert after["catalog"]["production_gpu"] and "scat-gbar-v23" in after["catalog"]["model_ids"]
    roles = ["platform", "actions", "worker", "hermes-main", "hermes-worker"]
    for role in roles:
        c = after["containers"][role]
        assert c["flags"]["SCAT_GPU_CONFIG_FILE"] == "var/gpu/production.json"
        assert len(c["gpu_mounts"]) == 2
        assert {m["Destination"]: m["RW"] for m in c["gpu_mounts"]} == {
            str(ROOT / "var/gpu"): True,
            str(ROOT / "var/gpu/production.json"): False,
        }
    skills = [
        after["containers"][role]["skills"] for role in ["platform", "hermes-main", "hermes-worker"]
    ]
    assert skills[0] == skills[1] == skills[2]
    assert skills[0]["versions"]["scat-experiment"] == "0.2.0"
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
    report = {
        "status": "passed",
        "release": after["release"],
        "server_not_rebooted": True,
        "gpu_container_and_metadata_unchanged": True,
        "five_core_gpu_mounts_verified": True,
        "native_skills_consistent": True,
        "cases": {},
    }
    total = {
        "llm_calls": 0,
        "tool_calls": 0,
        "charged_tokens": 0,
        "provider_input_tokens": 0,
        "provider_output_tokens": 0,
    }
    trusted_operations = 0
    tasks_path = ROOT / "var/deploy/h7b/shared/state/tasks.sqlite"
    gpu_path = ROOT / "var/gpu/requests.sqlite"
    for case in ["forecast", "backtest"]:
        receipt = json.loads((OUT / (case + ".json")).read_text())
        assert receipt["status"] == "passed"
        with db(tasks_path) as conn:
            row = conn.execute(
                "SELECT * FROM model_tasks WHERE task_id=?", (receipt["task_id"],)
            ).fetchone()
            snapshot = json.loads(row["snapshot"])
            assert digest(snapshot) == row["sha256"] == receipt["approved_snapshot"]
            frozen = json.loads(
                (inputs / (snapshot["workflow"]["input_sha256"] + ".json")).read_text()
            )
            assert digest(frozen) == snapshot["workflow"]["input_sha256"]
            assert len(frozen["flights"]) == 1 and frozen["synthetic"] is False
            flight = frozen["flights"][0]
            assert len(flight["gbar_windows"]) == 1 and not flight["gbar_excluded"]
            window = flight["gbar_windows"][0]
            assert window["annotations"]["model_resampled_points"] == 36
            assert snapshot["workflow"]["gpu"] == after["catalog"]["gpu_registration"]
            executions = conn.execute(
                "SELECT * FROM skill_backend_executions WHERE task_id=?", (receipt["task_id"],)
            ).fetchall()
            assert (
                len(executions) == 1
                and executions[0]["calls"] == 1
                and executions[0]["state"] == "succeeded"
            )
            output = json.loads(executions[0]["output"])
            assert digest(output) == executions[0]["output_sha"]
            assert all(math.isfinite(s["error_m"]) for s in output["samples"])
            for artifact in conn.execute(
                "SELECT body,sha FROM model_step_artifacts WHERE scope=? AND run=?",
                (row["scope"], row["run_id"]),
            ):
                assert digest(json.loads(artifact["body"])) == artifact["sha"]
            job = conn.execute(
                "SELECT state,parent_imported FROM skill_jobs WHERE task_id=?",
                (receipt["task_id"],),
            ).fetchone()
            assert job["state"] == "succeeded"
            sessions = conn.execute(
                "SELECT count(*) FROM skill_job_sessions WHERE task_id=?", (receipt["task_id"],)
            ).fetchone()[0]
            assert sessions == (2 if case == "forecast" else 1)
            assert (
                conn.execute(
                    "SELECT count(*) FROM skill_job_events WHERE task_id=? AND kind='native_run_bound'",
                    (receipt["task_id"],),
                ).fetchone()[0]
                == 1
            )
            if case == "forecast":
                repair_event = conn.execute(
                    "SELECT id FROM skill_job_events WHERE task_id=? AND kind='operator_recovered_null_truth_postprocessing'",
                    (receipt["task_id"],),
                ).fetchone()[0]
                recovery_operations = conn.execute(
                    "SELECT count(*) FROM skill_job_events WHERE task_id=? AND kind='step_returned' AND id>?",
                    (receipt["task_id"], repair_event),
                ).fetchone()[0]
                assert recovery_operations == 6
                trusted_operations += recovery_operations
                assert len(window["timestamps"]) == len(flight["data"]["plots"]) == 200
                assert not window["annotations"]["actual_future_candidates"]
                assert not output["samples"] and output["prediction_count"] == 3
                assert (
                    output["analysis_result"]["context"]["metric_availability"]
                    == "no_verified_truth"
                )
                assert (
                    job["parent_imported"] == 1
                    and snapshot["skills"]["parent_budget"]["run_id"] == receipt["planning_run_id"]
                )
            else:
                assert (
                    len(output["samples"]) == 3
                    and window["annotations"]["actual_future_candidates"]
                )
                assert output["analysis_result"]["context"]["metric_availability"] == "available"
            budget = dict(
                conn.execute(
                    "SELECT * FROM skill_run_budgets WHERE run_id=?", (row["run_id"],)
                ).fetchone()
            )
            assert not budget["exhausted"]
            for field in total:
                total[field] += budget[field]
        with db(gpu_path) as conn:
            gpu = conn.execute(
                "SELECT * FROM gpu_requests WHERE task=?", (receipt["task_id"],)
            ).fetchall()
            assert len(gpu) == 1 and gpu[0]["calls"] == 1 and gpu[0]["state"] == "succeeded"
            actual = json.loads(gpu[0]["output"])
            assert digest(actual) == gpu[0]["output_sha"]
            series = actual["payload"]["series"]
            assert len(series) == 1
            assert datetime.fromisoformat(series[0]["forecast_origin"]) == datetime.fromisoformat(
                window["timestamps"][-1]
            )
            if case == "forecast":
                assert all(v is None for r in series[0]["expected_values"] for v in r)
        report["cases"][case] = {
            "task_id": receipt["task_id"],
            "thread_id": receipt["thread_id"],
            "gpu_requests": 1,
            "calls": 1,
            "raw_window_points": len(window["timestamps"]),
            "model_resampled_points": 36,
            "metric_samples": len(output["samples"]),
            "artifact_count": len(receipt["artifact_ids"]),
            "snapshot_and_step_hashes_verified": True,
            "original_origin_verified": True,
            "independent_native_sessions": 1,
            "trusted_report_recovery_sessions": 1 if case == "forecast" else 0,
        }
    assert total["llm_calls"] <= 48 and total["charged_tokens"] <= 2000000
    assert len(after["gpu_receipts"]) == len(before["gpu_receipts"]) + 2
    report["budget_usage_including_planning_once"] = total
    report["trusted_recovery_operations_outside_native_tool_counter"] = trusted_operations
    report["all_tool_and_recovery_operations"] = total["tool_calls"] + trusted_operations
    report["actual_gpu_posts"] = 2
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    with db(tasks_path) as conn:
        r = conn.execute(
            "SELECT snapshot FROM model_tasks WHERE task_id=?",
            (json.loads((OUT / "backtest.json").read_text())["task_id"],),
        ).fetchone()
        expected_sources = json.loads(r[0])["skills"]["sources"]
    source_counts = {}
    for role, python in [("platform", "scat-agent"), ("hermes-main", "scat-hermes")]:
        code = (
            "import json,hashlib;from pathlib import Path;r=Path.cwd();print(json.dumps({p:hashlib.sha256((r/p).read_bytes()).hexdigest() for p in "
            + repr(list(expected_sources))
            + "}))"
        )
        current = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "scat-hermes-h7b-" + role + "-1",
                    "/opt/conda/envs/" + python + "/bin/python",
                    "-B",
                    "-c",
                    code,
                ],
                text=True,
            )
        )
        assert current == expected_sources
        source_counts[role] = len(current)
    report["approved_implementation_fingerprints_matched"] = source_counts
    assert after["release"] == plan["release"]
    report["standard_gpu_image_migration"] = False
    (OUT / "acceptance-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
