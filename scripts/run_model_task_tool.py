"""Private JSON stdin bridge for Hermes task tools; never accepts confirm/submit."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def schemas():
    from pydantic import BaseModel, ConfigDict, Field

    from application.model_tasks import TaskProposal

    class Empty(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Prepare(Empty):
        proposal: TaskProposal

    class Get(Empty):
        task_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    class Artifact(Get):
        artifact_id: str = Field(
            min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
        )

    return {
        "scat_task_catalog": ("查询注册模型、支持数据集、资源限制和版本。", Empty),
        "scat_task_prepare": (
            "校验并准备模型任务，展示参数后等待用户点击确认；此工具不会执行或提交计算。",
            Prepare,
        ),
        "scat_task_list": ("列出当前可信会话的任务，可在对话恢复后查询。", Empty),
        "scat_task_get": ("查询当前会话一个任务的状态和结果引用；unknown 状态不得重试执行。", Get),
        "scat_task_artifact": ("获取当前会话任务的已保存分析结果。", Artifact),
    }


async def invoke(payload):
    from application.model_tasks import ModelTasks
    from application.task_store import TaskStore
    from core import settings
    from core.settings import AnalysisIdentityMode
    from hermes_runtime.registry import RunRegistry
    from scat.infrastructure.database import flight_database
    from scat_model_runtime.production import (
        configure_production_model_runtime,
        shutdown_production_model_runtime,
    )
    from service.hermes_app import project_path

    if settings.ANALYSIS_IDENTITY_MODE != AnalysisIdentityMode.SINGLE_USER:
        raise ValueError("Task profile requires single-user identity")
    operation = payload["operation"]
    definitions = schemas()
    if operation not in definitions:
        raise ValueError("Unknown operation")
    args = definitions[operation][1].model_validate(payload["arguments"])
    registry = RunRegistry(project_path(settings.HERMES_STATE_FILE), workspace_root=ROOT)
    context = registry.require_control_scope(payload["scope"], payload["source_run_id"])
    configure_production_model_runtime(settings)
    app = ModelTasks(TaskStore(project_path(settings.HERMES_TASK_STATE_FILE), workspace_root=ROOT))
    if os.environ.get("SCAT_SKILL_JOBS") == "1":
        from application.skill_job_factory import create_skill_tasks

        app = create_skill_tasks(
            ROOT,
            project_path(settings.HERMES_TASK_STATE_FILE),
            cpu_fixture=os.environ.get("SCAT_S4_CPU_FIXTURE") == "1",
            parent_budget_path=os.environ.get("SCAT_PARENT_BUDGET_PATH"),
        )
    try:
        if operation == "scat_task_catalog":
            data = {"items": app.catalog()}
        elif operation == "scat_task_prepare":
            data = app.prepare(
                context,
                args.proposal,
                proposal_key=payload["proposal_key"],
                source_run_id=payload["source_run_id"],
            )
        elif operation == "scat_task_list":
            data = {
                "items": [
                    await app.get(context, row["task_id"])
                    for row in app.store.list(context.scope_key)
                ]
            }
        elif operation == "scat_task_get":
            data = await app.get(context, args.task_id)
        else:
            data = await app.artifact(context, args.task_id, args.artifact_id)
        if len(json.dumps(data, ensure_ascii=False).encode()) > 65536:
            data = {
                "task_id": getattr(args, "task_id", None),
                "message": "Result exceeds tool output limit; use the scoped HTTP task/artifact endpoint",
            }
        return {
            "status": "awaiting_confirmation" if operation == "scat_task_prepare" else "ok",
            "data": data,
        }
    finally:
        shutdown_production_model_runtime()
        await flight_database.close()


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use Anaconda Python")
    os.chdir(ROOT)
    if sys.argv[1:] == ["--schemas"]:
        data = [
            {"name": name, "description": description, "parameters": schema.model_json_schema()}
            for name, (description, schema) in schemas().items()
        ]
    else:
        try:
            raw = sys.stdin.read(262145)
            if len(raw) > 262144:
                raise ValueError("Input exceeds limit")
            data = asyncio.run(invoke(json.loads(raw)))
        except Exception as exc:  # noqa: BLE001 -- private bridge returns sanitized failures
            data = {
                "status": "rejected",
                "error_type": type(exc).__name__,
                "message": "Task request rejected or backend unavailable; inspect the current task without resubmitting execution",
            }
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
