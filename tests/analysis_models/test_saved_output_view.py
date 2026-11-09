import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from application.context import ExecutionContext
from application.model_output_view import saved_model_output
from application.task_store import TaskConflict, TaskNotFound, digest
from gbar_core.transport import FEATURES, OUTPUT_UNITS
from scat_model_runtime.backends.base import InferenceResult
from tests.analysis_models.test_regression_analysis import _output, _prediction


def test_saved_output_is_scoped_immutable_repeatable_and_cannot_use_tampered_receipts(tmp_path):
    context = ExecutionContext("owner", "thread")
    snapshot = {
        "workflow": {
            "proposal": {
                "kind": "forecast",
                "models": [{"model": "scat-gbar-v23", "version": "23"}],
                "horizons_s": [10.0],
            }
        }
    }

    def get(scope, task):
        if scope != context.scope_key or task != "task":
            raise TaskNotFound()
        return {"run_id": "a" * 32, "snapshot": snapshot, "sha256": digest(snapshot)}

    tasks = SimpleNamespace(store=SimpleNamespace(get=get))
    series = _prediction("sample")
    series.annotations["class_probabilities"] = [[0.1, 0.7, 0.2]]
    series = series.model_copy(
        update={"values": tuple((*row, 100.0, 0.0, 0.0) for row in series.values)}
    )
    output = _output(_prediction("sample")).model_copy(
        update={
            "model_key": "scat-gbar-v23",
            "model_version": "23",
            "target_names": FEATURES,
            "target_units": OUTPUT_UNITS,
            "series": (series,),
        }
    )
    raw = InferenceResult(
        request_id="receipt",
        run_id="a" * 32,
        model_key="scat-gbar-v23",
        model_version="23",
        input_schema="regression-task-input/v1",
        output_schema="regression-task-output/v1",
        payload=output.model_dump(mode="json"),
    ).model_dump(mode="json")
    path = tmp_path / "var/gpu/requests.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE gpu_requests(id TEXT,task TEXT,state TEXT,output TEXT,output_sha TEXT,created REAL)"
        )
        db.execute(
            "INSERT INTO gpu_requests VALUES(?,?,?,?,?,?)",
            ("receipt", "task", "succeeded", json.dumps(raw), digest(raw), 1),
        )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first = saved_model_output(context, tasks, "task", root=tmp_path)
    assert first == saved_model_output(context, tasks, "task", root=tmp_path)
    assert first["new_model_calls"] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert {"classification", "prediction", "action_link"}.issubset(
        {b["type"] for b in first["analysis_result"]["blocks"]}
    )
    with pytest.raises(TaskNotFound):
        saved_model_output(ExecutionContext("other", "thread"), tasks, "task", root=tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gpu_requests SET output_sha=?", ("f" * 64,))
    with pytest.raises(TaskConflict, match="integrity"):
        saved_model_output(context, tasks, "task", root=tmp_path)
