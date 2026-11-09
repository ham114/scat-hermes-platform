"""Private workflow bridge; no approval, shell or identity fields in model schemas."""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def validation_feedback(exc):
    """Expose schema paths and fixed rules, never rejected values or private exception text."""
    from workflow_runtime.contracts import TrialConfiguration, WorkflowProposal

    allowed = {"proposal", *WorkflowProposal.model_fields, *TrialConfiguration.model_fields}
    feedback = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False)[:8]:
        path = [
            str(p) if isinstance(p, int) or p in allowed else "unknown_field" for p in error["loc"]
        ]
        item = {"field": ".".join(path), "rule": error["type"]}
        if (
            error["msg"]
            == "Value error, GBAR requires min_observed_s >= 350 and horizons <= 360 seconds"
        ):
            item.update(
                field="proposal.min_observed_s / proposal.horizons_s",
                rule="gbar_window_limits",
                message="GBAR观测至少350秒、预测时域不超过360秒；只修正这些字段，不改变航班或预测模式。",
            )
        feedback.append(item)
    return feedback


async def invoke(payload):
    from application.model_step_store import StepError
    from application.skill_job_factory import create_skill_tasks
    from core import settings
    from core.settings import AnalysisIdentityMode
    from hermes_runtime.registry import RunRegistry
    from scat.infrastructure.database import flight_database
    from service.hermes_app import project_path
    from workflow_runtime.catalog import CONTROLS, OPERATIONS, describe

    if settings.ANALYSIS_IDENTITY_MODE != AnalysisIdentityMode.SINGLE_USER:
        raise StepError("single_user_profile_required")
    name = payload["operation"]
    if name in OPERATIONS:
        if not payload.get("job_session"):
            raise StepError("approved_workflow_job_required")
        app = create_skill_tasks(ROOT, Path(os.environ["SCAT_SKILL_JOB_STATE"]))
        return await app.skills.invoke(
            payload["job_session"],
            name,
            payload["arguments"],
            scope=payload["scope"],
            native_run=payload["source_run_id"],
        )
    if name not in CONTROLS or os.environ.get("SCAT_S5_WORKFLOWS") != "1":
        raise StepError("workflow_profile_required")
    args = CONTROLS[name][0].model_validate(payload["arguments"])
    context = RunRegistry(
        project_path(settings.HERMES_STATE_FILE), workspace_root=ROOT
    ).require_control_scope(payload["scope"], payload["source_run_id"])
    app = create_skill_tasks(
        ROOT,
        project_path(settings.HERMES_TASK_STATE_FILE),
        parent_budget_path=os.environ.get("SCAT_PARENT_BUDGET_PATH"),
    )
    try:
        if name == "scat_workflow_catalog":
            data = describe()
        elif name == "scat_workflow_prepare":
            data = await app.prepare_workflow(
                context,
                args.proposal,
                proposal_key=payload["proposal_key"],
                source_run_id=payload["source_run_id"],
            )
        elif name == "scat_workflow_list":
            data = {
                "items": [
                    await app.get(context, r["task_id"])
                    for r in app.store.list(context.scope_key)
                    if "workflow" in r["snapshot"]
                ]
            }
        else:
            row = app.store.get(context.scope_key, args.task_id)
            if "workflow" not in row["snapshot"]:
                raise StepError("workflow_task_required")
            data = (
                await app.get(context, args.task_id)
                if name == "scat_workflow_get"
                else await app.artifact(context, args.task_id, args.artifact_id)
            )
        if len(json.dumps(data, ensure_ascii=False).encode()) > 65536:
            data = {
                "task_id": getattr(args, "task_id", None),
                "message": "Use the scoped HTTP task/artifact endpoint for the full result",
            }
        return {
            "status": "awaiting_confirmation" if name == "scat_workflow_prepare" else "ok",
            "data": data,
        }
    finally:
        await flight_database.close()


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    from workflow_runtime.catalog import schemas

    if sys.argv[1:] == ["--schemas"]:
        output = schemas(controls=os.environ.get("SCAT_WORKFLOW_CONTROLS") == "1")
    else:
        try:
            raw = sys.stdin.read(131073)
            if len(raw) > 131072:
                raise ValueError("Request too large")
            output = asyncio.run(invoke(json.loads(raw)))
            if {"agents.model_agent.graph", "agents.model_tools", "agents.scat_agent"}.intersection(
                sys.modules
            ):
                raise RuntimeError("legacy_graph_import")
        except Exception as exc:  # noqa: BLE001 -- sanitize private bridge errors
            from application.model_step_store import StepError

            output = {
                "status": "rejected",
                "error": str(exc) if isinstance(exc, StepError) else "workflow_request_rejected",
                "error_type": type(exc).__name__,
            }
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                output["validation_errors"] = validation_feedback(exc)
                output["recovery"] = "仅按字段错误修正一次；仍失败则停止准备，不重复变更无关字段。"
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
