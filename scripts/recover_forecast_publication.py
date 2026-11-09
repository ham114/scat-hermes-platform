"""Inspect/retry one already verified publication; no native agent or numerical execution."""

import asyncio
import json
import sqlite3
from pathlib import Path

from application.skill_job_factory import create_skill_tasks
from application.skill_worker import completion_reference, publish_result
from application.task_store import digest


async def main():
    root = Path.cwd()
    task = "f1d867ad59894bd1b4e1b894c55e49c3"
    path = root / "var/deploy/h7b/shared/state/tasks.sqlite"
    app = create_skill_tasks(root, path, workflows_enabled=True, workflow_synthetic=False)
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        scope = db.execute("SELECT scope FROM model_tasks WHERE task_id=?", (task,)).fetchone()[0]
        executions = db.execute(
            "SELECT state,calls FROM skill_backend_executions WHERE task_id=?", (task,)
        ).fetchall()
    assert executions == [("succeeded", 1)]
    row = app.store.get(scope, task)
    assert digest(row["snapshot"]) == row["sha256"]
    assert row["snapshot"]["skills"]["sources"] == app.skills.sources()
    reference = completion_reference(app, row)
    assert reference is not None
    await app.artifacts.ensure_indexes()
    indexes = await app.artifacts.collection.index_information()
    report = {
        "task_id": task,
        "backend_calls": 1,
        "prediction_calls_in_recovery": 0,
        "indexes": {
            name: {
                k: value
                for k, value in spec.items()
                if k in {"key", "unique", "expireAfterSeconds"}
            }
            for name, spec in indexes.items()
        },
    }
    try:
        await publish_result(app, row, reference)
        report["publication"] = "succeeded"
    except Exception as exc:
        report["publication"] = "failed"
        report["exception_type"] = type(exc).__name__
        report["exception_message"] = str(exc)[:500]
    print(json.dumps(report))


asyncio.run(main())
