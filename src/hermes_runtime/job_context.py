"""Lightweight native job binding; numerical components stay in the APP Conda process."""

import hashlib
from contextlib import closing
from pathlib import Path

from application.skill_job_store import SkillJobStore
from application.task_store import TaskConflict, TaskStore
from hermes_runtime.release_store import resolve_release


def load_approved_job(root, state, task_id, worker, generation):
    root = Path(root).resolve()
    jobs = SkillJobStore(TaskStore(Path(state), workspace_root=root))
    with closing(jobs.tasks.connect()) as db:
        record = db.execute("SELECT scope FROM model_tasks WHERE task_id=?", (task_id,)).fetchone()
    if record is None:
        raise TaskConflict("Approved background job missing")
    row = jobs.get(record[0], task_id)
    jobs.require_lease(task_id, worker, generation)
    snapshot = row["snapshot"]["skills"]
    try:
        release = resolve_release(root, snapshot["bundle_sha256"])
    except (ValueError, OSError):
        raise TaskConflict("Approved background skill release unavailable") from None
    if snapshot["bundle_sha256"] != release.bundle_sha256 or snapshot["expires"] <= jobs.clock():
        raise TaskConflict("Approved skill changed or expired")
    files = snapshot["sources"] | {
        "services/hermes/model-operations.v1.json": snapshot["operation_catalog_sha256"]
    }
    if "workflow" in row["snapshot"]:
        files["services/hermes/workflow-operations.v1.json"] = row["snapshot"]["workflow"][
            "catalog_sha256"
        ]
    for name, expected in files.items():
        path = (root / name).resolve()
        if (
            not path.is_relative_to(root)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise TaskConflict("Approved implementation changed")
    # The APP bridge repeats full component/input checks immediately before each operation.
    return jobs, row
