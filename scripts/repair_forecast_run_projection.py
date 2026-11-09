"""Run inside platform: restore one approved time projection, never recompute a forecast."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bson import BSON

from analysis_runs.contracts import AnalysisRunRecord
from analysis_runs.repository import analysis_run_repository
from application.skill_job_factory import create_skill_tasks
from application.skill_worker import completion_reference
from application.task_store import digest
from workflow_runtime.data import load_input

ROOT = Path.cwd()
OUT = ROOT / "var/validation/20261107-forecast-contract"
TASK = "f1d867ad59894bd1b4e1b894c55e49c3"


async def main():
    path = ROOT / "var/deploy/h7b/shared/state/tasks.sqlite"
    app = create_skill_tasks(ROOT, path, workflows_enabled=True, workflow_synthetic=False)
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        scope = db.execute("SELECT scope FROM model_tasks WHERE task_id=?", (TASK,)).fetchone()[0]
        executions = db.execute(
            "SELECT state,calls FROM skill_backend_executions WHERE task_id=?", (TASK,)
        ).fetchall()
    assert executions == [("succeeded", 1)]
    row = app.store.get(scope, TASK)
    snapshot = row["snapshot"]
    assert digest(snapshot) == row["sha256"]
    assert snapshot["skills"]["sources"] == app.skills.sources()
    reference = completion_reference(app, row)
    assert reference is not None
    frozen = load_input(ROOT, snapshot["workflow"]["input_sha256"], scope=scope)
    assert (
        len(frozen["flights"]) == 1
        and frozen["flights"][0]["observation_windows"][0]["point_count"] == 200
    )
    saved = snapshot["run"]
    expected = saved["input_refs"]
    repo = analysis_run_repository
    await repo.ensure_indexes()
    condition = {
        "run_id": saved["run_id"],
        "owner_user_id": saved["owner_user_id"],
        "thread_id": saved["thread_id"],
    }
    document = await repo.collection.find_one(condition)
    assert document is not None
    original = document["input_refs"]
    if original != expected:
        assert len(original) == len(expected) == 1
        for old, approved in zip(original, expected, strict=True):
            for key in set(old) | set(approved):
                if key in {"start_time", "end_time"}:
                    actual = old[key]
                    assert isinstance(actual, datetime)
                    if actual.tzinfo is None:
                        actual = actual.replace(tzinfo=UTC)
                    precise = datetime.fromisoformat(approved[key])
                    assert actual == precise.replace(
                        microsecond=(precise.microsecond // 1000) * 1000
                    )
                else:
                    assert old.get(key) == approved.get(key)
        backup = OUT / "state-before-projection-repair"
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup.chmod(0o700)
        target = backup / (saved["run_id"] + "-input-refs.bson")
        with target.open("xb") as stream:
            target.chmod(0o600)
            stream.write(BSON.encode({**condition, "input_refs": original}))
        changed = await repo.collection.update_one(
            {**condition, "input_refs": original}, {"$set": {"input_refs": expected}}
        )
        assert changed.modified_count == 1
    restored = await repo.collection.find_one(condition)
    AnalysisRunRecord.from_mongo_document(restored)
    assert restored["input_refs"] == expected
    unchanged = app.store.get(scope, TASK)
    assert unchanged["sha256"] == row["sha256"] and unchanged["snapshot"] == snapshot
    report = {
        "status": "passed",
        "task_id": TASK,
        "run_id": saved["run_id"],
        "only_input_ref_serialization_repaired": True,
        "approved_snapshot_and_frozen_input_unchanged": True,
        "precision_restored_from_approved_snapshot": True,
        "verified_result_already_exists": True,
        "backend_calls_before": 1,
        "prediction_calls_in_repair": 0,
        "private_bson_backup_server_only": True,
    }
    (OUT / "projection-repair.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


asyncio.run(main())
