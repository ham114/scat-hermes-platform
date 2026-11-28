"""Private trusted JSON bridge for autonomous chart admission and readback."""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def schemas():
    from pydantic import Field

    from visualization_runtime.contracts import (
        Contract,
        CreateVisualization,
        Identity,
        ReviseVisualization,
    )

    class Revise(ReviseVisualization):
        artifact_id: Identity

    class Get(Contract):
        task_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    return {
        "create_visualization": (
            "根据实际 data_refs 委派自主绘图。intent应是结合相关对话后的完整任务说明，保留仍适用的指标、单位和用户约定，不能只转发最后一句；作用范围通过自然语言说明，不添加scope参数。换数据时重新查询并使用新的真实引用，不添加未要求的指标。先用list_visualization_sources取得kind/source_id/run_id/payload_sha256；不传代码。只表示入队，审核后才交付。request_id使用唯一UUID，重试保留原号。",
            CreateVisualization,
        ),
        "revise_visualization": (
            "按用户反馈委派修改指定artifact_id图表。feedback应结合相关对话说明本轮完整修改要求；仅本次、以后默认等含义用自然语言表达。新版本保留旧图和原数据，换航班等数据变化使用create并取得新数据引用。保持request_id幂等，不自行填写审核结论。",
            Revise,
        ),
        "get_visualization_status": (
            "查询当前会话图表任务与已审核产物引用；失败或待核实时不能声称交付。",
            Get,
        ),
        "list_visualizations": (
            "读取当前会话已有图表及任务，修改前选用用户指定的 artifact_id；不调用绘图模型。",
            Contract,
        ),
        "list_visualization_sources": (
            "列出当前会话最近查询的真实数据回执，返回 create_visualization 所需的 data_refs，含真实 run_id/source_id/payload_sha256。",
            Contract,
        ),
    }


async def invoke(payload):
    from analysis_runs import analysis_artifact_repository
    from application.visualization_tasks import create_visualization_tasks
    from core import settings
    from hermes_runtime.registry import RunRegistry
    from hermes_runtime.results import ToolResultStore
    from visualization_runtime.paths import confined_path
    from visualization_runtime.settings import load_settings

    path = lambda value: confined_path(ROOT, Path(value))
    registry = RunRegistry(path(settings.HERMES_STATE_FILE), workspace_root=ROOT)
    context = registry.require_control_scope(payload["scope"], payload["source_run_id"])
    operation = payload["operation"]
    args = schemas()[operation][1].model_validate(payload["arguments"])
    delegation = {}
    if operation in {"create_visualization", "revise_visualization"}:
        delegation = trusted_delegation(registry, context, payload["source_run_id"])
    visualization_settings = load_settings(ROOT)
    parent_budget_path = (
        os.environ.get("SCAT_PARENT_BUDGET_PATH") or visualization_settings.parent_budget_path
    )
    app = create_visualization_tasks(
        ROOT,
        path(settings.HERMES_TASK_STATE_FILE),
        visualization_settings,
        ToolResultStore(path(settings.HERMES_RESULT_STATE_FILE), workspace_root=ROOT),
        artifacts=analysis_artifact_repository,
        parent_budget_path=path(parent_budget_path) if parent_budget_path else None,
        read_only=True,
        execution_credentials=False,
    )
    if operation == "list_visualization_sources":
        return {"status": "ok", "data": await app.sources(context, registry)}
    if operation == "create_visualization":
        args = memory_token(app, context, args)
        for ref in args.data_refs:
            if ref.kind == "readonly_result":
                registry.require(context, ref.run_id)
        row = await app.create(
            context, args, parent_run_id=payload["source_run_id"], learn=True, **delegation
        )
        data = app.view(context, row["task_id"])
    elif operation == "revise_visualization":
        args = memory_token(app, context, args)
        row = app.revise(
            context,
            args.artifact_id,
            args,
            parent_run_id=payload["source_run_id"],
            learn=True,
            **delegation,
        )
        data = app.view(context, row["task_id"])
    elif operation == "get_visualization_status":
        data = app.view(context, args.task_id)
    else:
        data = app.list(context)
    from fastapi.encoders import jsonable_encoder

    return {"status": "ok", "data": jsonable_encoder(data)}


def trusted_delegation(registry, context, run_id):
    raw = registry.original_question(context, run_id)
    history = registry.delegation_history(context, run_id)
    return {
        "user_message": raw,
        "conversation": history,
        "source_turn_id": history[-1]["source_id"],
    }


def memory_token(app, context, args):
    if app.preferences:
        state = app.preferences.list(context.user_id)
        return args.model_copy(
            update={"memory_epoch": state["epoch"], "memory_revision": state["revision"]}
        )
    return args


def main():
    if sys.argv[1:] == ["--schemas"]:
        result = [
            {"name": name, "description": description, "parameters": schema.model_json_schema()}
            for name, (description, schema) in schemas().items()
        ]
    else:
        try:
            raw = sys.stdin.read(131073)
            if len(raw) > 131072:
                raise ValueError("input_limit")
            result = asyncio.run(invoke(json.loads(raw)))
        except Exception:  # noqa: BLE001 -- never expose provider/configuration errors
            result = {
                "status": "rejected",
                "error": "visualization_request_unavailable",
                "message": "查询已有图表任务后再处理；检查数据引用、功能开关和父预算，不自动重复创建。",
            }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
