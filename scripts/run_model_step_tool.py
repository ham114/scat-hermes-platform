"""Private coarse-operation bridge. The opt-in S3 fixture never reads business data."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def schemas():
    from pydantic import BaseModel, ConfigDict

    from application.model_step_store import StepReference
    from application.model_steps import OPERATIONS

    class Empty(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Referenced(Empty):
        reference: StepReference

    descriptions = (
        "准备可信任务已固定的模型输入；不接受模型、数据或 confirmed 等聊天授权参数。",
        "用 input 引用执行可信任务已批准的注册模型；unknown 不得自动重试。",
        "用 prediction 引用及固定版本评估器计算误差。",
        "用 evaluation 引用保存标准 AnalysisResult。",
        "用 result 引用读取当前可信运行的分析结果、单位与时间轴。",
    )
    return {
        name: (desc, Empty if info[0] is None else Referenced)
        for (name, info), desc in zip(OPERATIONS.items(), descriptions, strict=True)
    }


async def invoke(payload):
    from application.model_step_store import ModelStepStore, StepError
    from application.model_steps import ModelSteps
    from scat_model_runtime.cpu_fixture import fixture_runtime

    definitions = schemas()
    name = payload["operation"]
    if name not in definitions:
        raise StepError("unknown_model_operation")
    arguments = definitions[name][1].model_validate(payload["arguments"]).model_dump(mode="json")
    if payload.get("job_session"):
        from application.skill_jobs import SkillJobs
        from application.task_store import TaskStore

        path = Path(os.environ["SCAT_SKILL_JOB_STATE"])
        jobs = SkillJobs(TaskStore(path, workspace_root=ROOT), ROOT)
        result = await jobs.invoke(
            payload["job_session"],
            name,
            arguments,
            scope=payload["scope"],
            native_run=payload["source_run_id"],
        )
        if {"agents.model_agent.graph", "agents.model_tools", "agents.scat_agent"}.intersection(
            sys.modules
        ):
            raise StepError("legacy_graph_import_in_step_backend")
        return result
    # This environment switch belongs to the launcher, never to the tool arguments.
    if os.environ.get("SCAT_S3_CPU_FIXTURE") != "1":
        raise StepError("approved_job_required_s4")
    home = Path(os.environ["SCAT_MODEL_STEP_HOME"]).resolve()
    if not home.is_relative_to((ROOT / ".tmp/s3").resolve()):
        raise StepError("synthetic_profile_outside_s3_project_directory")
    scope, run_id = payload["scope"], payload["source_run_id"]
    if not scope or not run_id:
        raise StepError("trusted_step_context_required")
    # Persist a controller-owned fixed deadline across subprocess calls; reading/retries do not renew it.
    deadline_path = home / "fixture-deadline.json"
    import time

    try:
        with deadline_path.open("x", encoding="utf-8") as stream:
            json.dump({"expires": time.time() + 600}, stream)
        deadline_path.chmod(0o600)
    except FileExistsError:
        pass
    expires = json.loads(deadline_path.read_text())["expires"]
    kernel, grant, _ = fixture_runtime(scope, run_id, expires=expires)
    store = ModelStepStore(home / "model-steps.sqlite", workspace_root=ROOT)
    result = await ModelSteps(kernel, store, grant).invoke(name, arguments)
    if {"agents.model_agent.graph", "agents.model_tools", "agents.scat_agent"}.intersection(
        sys.modules
    ):
        raise StepError("legacy_graph_import_in_step_backend")
    result["execution_path"] = "neutral_model_steps/v1"
    return result


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    if sys.argv[1:] == ["--schemas"]:
        output = [
            {"name": n, "description": d, "parameters": s.model_json_schema()}
            for n, (d, s) in schemas().items()
        ]
    else:
        try:
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise ValueError("request_too_large")
            output = asyncio.run(invoke(json.loads(raw)))
        except Exception as exc:  # noqa: BLE001 -- private bridge exposes only fixed error codes
            from application.model_step_store import StepError

            output = {
                "status": "rejected",
                "error": str(exc) if isinstance(exc, StepError) else "model_step_rejected",
                "error_type": type(exc).__name__,
            }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
