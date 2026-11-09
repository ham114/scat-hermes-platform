"""Explicit application assembly for new skill jobs; legacy H5 assembly stays separate."""

from pathlib import Path

from application.skill_jobs import SkillJobs
from application.task_store import TaskStore
from application.workflow_tasks import WorkflowTasks


def create_skill_tasks(
    root,
    state,
    *,
    cpu_fixture=False,
    parent_budget_path=None,
    runs=None,
    artifacts=None,
    workflows_enabled=None,
    workflow_synthetic=None,
):
    root = Path(root).resolve()
    store = TaskStore(Path(state), workspace_root=root)
    skills = SkillJobs(store, root, parent_budget_path=parent_budget_path)
    models = skills.kernel()[0].models if cpu_fixture else None
    import os

    return WorkflowTasks(
        store,
        models=models,
        runs=runs,
        artifacts=artifacts,
        skills=skills,
        workflow_enabled=(
            os.environ.get("SCAT_S5_WORKFLOWS", "1") == "1"
            if workflows_enabled is None
            else workflows_enabled
        ),
        workflow_synthetic=(
            os.environ.get("SCAT_S5_SYNTHETIC") == "1"
            if workflow_synthetic is None
            else workflow_synthetic
        ),
    )
