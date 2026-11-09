"""Three single-send real inferences: migration fidelity, warm repeat and cold recovery."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from application.context import ExecutionContext
from application.task_store import TaskStore, digest
from gbar_core.transport import FEATURES, INPUT_UNITS
from scat_model_runtime.backends.base import InferenceRequest, InferenceResult
from scat_model_runtime.task_io import (
    RegressionInputSeries,
    RegressionTaskInput,
    RegressionTaskOutput,
)
from workflow_runtime.gpu import GPURegistration, GPURequests


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    os.umask(0o077)
    record = OUT / "gpu-conda-acceptance.json"
    resume = sys.argv[1:] == ["--resume-cached"]
    assert resume or not record.exists(), "already_admitted_no_replay"
    candidate = json.loads((OUT / "gpu-conda-candidate.json").read_text())
    assert candidate["status"] == "ready"
    tasks = TaskStore(ROOT / "var/deploy/h7b/shared/state/tasks.sqlite", workspace_root=ROOT)
    context = ExecutionContext("scat-researcher", "42297b89-54c3-469d-94c0-13bccae5b9e2")
    task = tasks.get(context.scope_key, "7b0dbcfd9cf846898ed3183b44a13e18")
    assert digest(task["snapshot"]) == task["sha256"]
    snapshot = task["snapshot"]["workflow"]
    info = json.loads(subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"]))[0]
    directory = Path(next(m["Source"] for m in info["Mounts"] if m["Destination"] == str(ROOT / "var/workflow-inputs")))
    frozen = json.loads((directory / (snapshot["input_sha256"] + ".json")).read_text())
    assert digest(frozen) == snapshot["input_sha256"] and frozen["synthetic"] is False
    windows = [RegressionInputSeries.model_validate(w) for f in frozen["flights"] for w in f["gbar_windows"]]
    assert len(windows) == 1 and len(windows[0].timestamps) == 200
    source = RegressionTaskInput(feature_names=FEATURES, feature_units=INPUT_UNITS, series=tuple(windows))
    with sqlite3.connect("file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True) as db:
        prior = db.execute("SELECT output,output_sha FROM gpu_requests WHERE task=? AND state='succeeded'", (task["task_id"],)).fetchall()
    assert len(prior) == 1
    previous = json.loads(prior[0][0])
    assert digest(previous) == prior[0][1]
    old = RegressionTaskOutput.model_validate(InferenceResult.model_validate(previous).payload)
    config = GPURegistration.model_validate({"enabled": True, "transport": {"endpoint": candidate["endpoint"], "release_id": candidate["release_id"], "max_connect_retries": 0}, "provenance": candidate["metadata"]["provenance"], "state_file": "var/gpu/readiness-conda-requests.sqlite"})
    ledger = GPURequests(ROOT, config)
    existing = ledger.records()
    assert (resume and len(existing) <= 3 and all(r["state"] == "succeeded" for r in existing)) or not existing
    report = {"status": "running", "case_scope": "one real approved frozen SCAT flight/window, not general scientific quality", "source_input_sha256": snapshot["input_sha256"], "reference_output_sha256": prior[0][1], "max_new_gpu_calls": 3, "calls": [], "production_switched": False}
    if resume:
        report = json.loads(record.read_text())
        assert report["status"] == "running"
        report["validation_resumed_from_persisted_success"] = True
    else:
        save(record, report)
    for index, label in enumerate(["migration_fidelity", "warm_repeat", "cold_recovery"]):
        backend = config.backend()
        try:
            identity = "readiness-conda-" + str(index) + "-" + snapshot["input_sha256"][:24]
            cached = next((r for r in existing if r["id"] == identity), None)
            if index == 2 and cached is None:
                start = time.monotonic()
                subprocess.run(["docker", "restart", "--time=30", candidate["container"]], capture_output=True, check=True, timeout=45)
                for _ in range(90):
                    if backend.health().ready:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("cold_recovery_failed")
                report["restart_to_ready_seconds"] = round(time.monotonic() - start, 3)
            assert backend.health().ready
            request = InferenceRequest(request_id=identity, run_id="readiness-conda-validation", model_key="scat-gbar-v23", model_version="23", input_schema=source.schema_version, output_schema="regression-task-output/v1", payload=source.model_dump(mode="json"))
            entry = next((e for e in report["calls"] if e["request_id"] == identity), None)
            if entry is None:
                entry = {"id": label, "request_id": identity, "status": "send_intent"}
                report["calls"].append(entry)
            save(record, report)
            start = time.monotonic()
            result = ledger.execute(backend, request, task="readiness-conda-validation", execution=label)
            elapsed = time.monotonic() - start
            output = RegressionTaskOutput.model_validate(result.payload)
            assert len(output.series) == len(old.series) == 1
            a, b = output.series[0], old.series[0]
            assert a.annotations["observed_trajectory"] == b.annotations["observed_trajectory"]
            assert a.annotations["observed_start_time"] == b.annotations["observed_start_time"] and a.annotations["observed_end_time"] == b.annotations["observed_end_time"]
            assert a.time_offsets_seconds == b.time_offsets_seconds and a.forecast_origin == b.forecast_origin
            actual, expected = np.asarray(a.values), np.asarray(b.values)
            assert np.isfinite(actual).all() and np.allclose(actual, expected, rtol=1e-6, atol=1e-3)
            pa = np.asarray(a.annotations["class_probabilities"])
            pb = np.asarray(b.annotations["class_probabilities"])
            assert np.isfinite(pa).all() and np.allclose(pa, pb, rtol=1e-6, atol=1e-7)
            save(OUT / "private" / ("gpu-conda-output-" + str(index) + ".json"), result.model_dump(mode="json"))
            entry.update(status="passed", seconds=round(cached["finished"] - cached["created"], 4) if cached else round(elapsed, 4), cached_receipt_reused=bool(cached), max_absolute_physical_delta=float(np.max(np.abs(actual - expected))), max_absolute_probability_delta=float(np.max(np.abs(pa - pb))), output_sha256=digest(result.model_dump(mode="json")))
            save(record, report)
        finally:
            backend.close()
    rows = ledger.records()
    assert len(rows) == 3 and all(r["state"] == "succeeded" and r["calls"] == 1 for r in rows)
    report.update(status="passed", new_gpu_calls=3, ledger=rows, receipt_single_send=True, cold_start_seconds=candidate["cold_start_seconds"])
    save(record, report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
