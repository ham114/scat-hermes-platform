"""Owner-scoped projection of persisted successful GPU receipts; no backend is called."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from agents.analysis_results import build_regression_model_analysis
from analysis_runs.artifact_contracts import AnalysisArtifactRecord
from application.task_store import TaskConflict, digest
from gbar_core.transport import FEATURES, OUTPUT_UNITS
from scat_model_runtime.backends.base import InferenceResult
from scat_model_runtime.task_io import RegressionTaskOutput
from schema.analysis import ActionLinkBlock

ROOT = Path(__file__).resolve().parents[2]


def saved_model_output(context, tasks, task_id, *, root=ROOT):
    task = tasks.store.get(context.scope_key, task_id)
    snapshot = task["snapshot"]
    if digest(snapshot) != task["sha256"]:
        raise TaskConflict("Approved snapshot integrity mismatch")
    proposal = snapshot.get("workflow", {}).get("proposal", {})
    if not any(
        m.get("model") == "scat-gbar-v23" and m.get("version") == "23"
        for m in proposal.get("models", [])
    ):
        raise TaskConflict("Saved output is outside the approved model selection")
    path = Path(root) / "var/gpu/requests.sqlite"
    if not path.is_file():
        raise TaskConflict("No persisted GPU output is available")
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id,output,output_sha FROM gpu_requests WHERE task=? AND state='succeeded' ORDER BY created LIMIT 33",
            (task_id,),
        ).fetchall()
    if not 1 <= len(rows) <= 32:
        raise TaskConflict("Saved model output is absent or exceeds display budget")
    outputs, sources = [], []
    for row in rows:
        raw = json.loads(row["output"])
        if digest(raw) != row["output_sha"]:
            raise TaskConflict("Saved model output integrity mismatch")
        result = InferenceResult.model_validate(raw)
        if (
            result.request_id != row["id"]
            or result.run_id != task["run_id"]
            or result.model_key != "scat-gbar-v23"
            or result.model_version != "23"
        ):
            raise TaskConflict("Saved model output identity mismatch")
        output = RegressionTaskOutput.model_validate(result.payload)
        if (
            output.model_key != result.model_key
            or output.model_version != result.model_version
            or output.target_names != FEATURES
            or output.target_units != OUTPUT_UNITS
        ):
            raise TaskConflict("Saved model payload identity mismatch")
        outputs.append(output)
        sources.append({"request_id": row["id"], "output_sha256": row["output_sha"]})
    output = outputs[0].model_copy(update={"series": tuple(s for o in outputs for s in o.series)})
    if proposal.get("kind") == "forecast":
        horizons = tuple(proposal["horizons_s"])
        cropped = []
        for series in output.series:
            indices = [series.time_offsets_seconds.index(h) for h in horizons]
            if any(v is not None for row in series.expected_values for v in row):
                raise TaskConflict("Future forecast cannot include unapproved truth")
            cropped.append(
                series.model_copy(
                    update={
                        "time_offsets_seconds": horizons,
                        "values": tuple(series.values[i] for i in indices),
                        "expected_values": (),
                    }
                )
            )
        output = output.model_copy(update={"series": tuple(cropped)})
    analysis = build_regression_model_analysis(output)
    analysis.title = "已保存GBAR模型输出（只读，不重新推理）"
    analysis.context.update(
        source_receipts_sha256=digest(sources),
        source_receipt_count=len(sources),
        display_only=True,
        approved_snapshot_sha256=task["sha256"],
        display_budget="表格最多100行、每块曲线最多200点、轨迹最多5000点；完整来源回执保留",
    )
    analysis.blocks.append(
        ActionLinkBlock(
            label="返回所属会话",
            url="/?" + urlencode({"thread_id": context.thread_id}),
            kind="back_to_chat",
            thread_id=context.thread_id,
        )
    )
    artifact_id = "model-output-" + task_id + "-" + digest(sources)[:16]
    import hashlib

    return {
        "artifact_id": artifact_id,
        "run_id": task["run_id"],
        "analysis_result": analysis.model_dump(mode="json"),
        "payload_sha256": hashlib.sha256(
            AnalysisArtifactRecord.canonical_payload(analysis)
        ).hexdigest(),
        "source_receipts": sources,
        "new_model_calls": 0,
    }
