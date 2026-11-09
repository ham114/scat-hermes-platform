"""Reviewed migration: keep artifact-ID uniqueness, allow verified pages in one run."""

import asyncio
import inspect
import json
import sqlite3
from pathlib import Path

from application.skill_job_factory import create_skill_tasks
from application.skill_worker import completion_reference
from application.task_store import digest


async def main():
    root = Path.cwd()
    identity = "f1d867ad59894bd1b4e1b894c55e49c3"
    path = root / "var/deploy/h7b/shared/state/tasks.sqlite"
    app = create_skill_tasks(root, path, workflows_enabled=True, workflow_synthetic=False)
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        scope = db.execute("SELECT scope FROM model_tasks WHERE task_id=?", (identity,)).fetchone()[
            0
        ]
        assert (
            db.execute(
                "SELECT count(*) FROM skill_backend_executions WHERE state IN ('prepared','running','unknown')"
            ).fetchone()[0]
            == 0
        )
        assert db.execute(
            "SELECT state,calls FROM skill_backend_executions WHERE task_id=?", (identity,)
        ).fetchall() == [("succeeded", 1)]
    row = app.store.get(scope, identity)
    assert digest(row["snapshot"]) == row["sha256"]
    assert app.skills.sources() == row["snapshot"]["skills"]["sources"]
    assert completion_reference(app, row) is not None
    implementation = inspect.getsource(type(app.artifacts).ensure_indexes)
    assert (
        "ix_analysis_artifact_run_kind" in implementation
        and "uq_analysis_artifact_run_kind" not in implementation
    )
    await app.artifacts.ensure_indexes()
    collection = app.artifacts.collection
    before = await collection.index_information()
    legacy = [
        name
        for name, item in before.items()
        if item.get("unique") and list(item.get("key", [])) == [("run_id", 1), ("kind", 1)]
    ]
    assert len(legacy) == 1 and legacy[0] == "uq_analysis_artifact_run_kind"
    assert before["uq_analysis_artifact_id"]["unique"] is True
    assert before["ix_analysis_artifact_run_kind"]["key"] == [("run_id", 1), ("kind", 1)]
    run = row["run_id"]
    other_count = await collection.count_documents({"run_id": {"$ne": run}})
    original = await collection.find(
        {"run_id": run}, {"artifact_id": 1, "payload_sha256": 1, "_id": 0}
    ).to_list(length=100)
    # Change only this derived-result lookup constraint. No raw collection or document is removed.
    await collection.drop_index(legacy[0])
    after = await collection.index_information()
    assert legacy[0] not in after
    assert all(after[name] == spec for name, spec in before.items() if name != legacy[0])
    assert await collection.count_documents({"run_id": {"$ne": run}}) == other_count
    for item in original:
        assert await collection.count_documents({"run_id": run, **item}) == 1
    report = {
        "status": "passed",
        "collection": collection.name,
        "removed_index": legacy[0],
        "before": {
            name: {k: v for k, v in spec.items() if k in {"key", "unique", "expireAfterSeconds"}}
            for name, spec in before.items()
        },
        "after": {
            name: {k: v for k, v in spec.items() if k in {"key", "unique", "expireAfterSeconds"}}
            for name, spec in after.items()
        },
        "unique_artifact_id_preserved": True,
        "owner_and_ttl_indexes_preserved": True,
        "documents_removed": 0,
        "unrelated_artifact_count_unchanged": True,
        "prediction_calls": 0,
        "approved_snapshot_and_input_unchanged": True,
    }
    print(json.dumps(report))


asyncio.run(main())
