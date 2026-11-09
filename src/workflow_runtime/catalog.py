"""Explicit native tool groups; schemas are served through the APP Conda bridge."""

from pydantic import Field

from application.model_step_store import StepReference
from workflow_runtime.contracts import Contract, WorkflowProposal


class Empty(Contract):
    pass


class Reference(Contract):
    reference: StepReference


class References(Contract):
    references: tuple[StepReference, ...] = Field(min_length=1, max_length=64)


class Materialize(Reference):
    kind: str = Field(
        pattern="^(batch_statistics|flight_features|phase_segments|prediction_metrics|prediction_metric_summaries)$"
    )


class Trial(Reference):
    trial_id: str = Field(pattern="^trial-[0-9a-f]{24}$")


class Prepare(Contract):
    proposal: WorkflowProposal


class Get(Contract):
    task_id: str = Field(pattern="^[0-9a-f]{32}$")


class Artifact(Get):
    artifact_id: str = Field(min_length=1, max_length=200, pattern="^[A-Za-z0-9._:-]+$")


# name -> (toolset, argument contract, description, output kind, effect)
OPERATIONS = {
    "scat_sample_prepare": (
        "scat_data_steps",
        Empty,
        "读取已批准的固定样本引用与质量覆盖，不重新抽样。",
        "input",
        "derived_write",
    ),
    "scat_materialize": (
        "scat_materialization_steps",
        Materialize,
        "按批准范围构建版本化派生结果；原始数据只读。",
        "prediction",
        "derived_write",
    ),
    "scat_materialization_read": (
        "scat_materialization_steps",
        Reference,
        "核实本任务派生版本、覆盖率和来源。",
        None,
        "query",
    ),
    "scat_trial_run": (
        "scat_experiment_steps",
        Trial,
        "执行批准清单中的一个稳定 trial；复用原后端号，未知不重跑。",
        "prediction",
        "compute",
    ),
    "scat_evaluation_summarize": (
        "scat_evaluation_steps",
        References,
        "核对批准 trial 并汇总各自与共同样本的指标、覆盖及阶段分组。",
        "evaluation",
        "compute",
    ),
    "scat_evaluation_diagnose": (
        "scat_evaluation_steps",
        Reference,
        "从实际评估制品提取最差案例、失败与排除原因。",
        "evaluation",
        "compute",
    ),
    "scat_report_build": (
        "scat_report_steps",
        References,
        "从本任务有效制品生成标准报告并保留可回查来源。",
        "result",
        "derived_write",
    ),
    "scat_report_read": (
        "scat_report_steps",
        Reference,
        "读取已保存的标准报告；实际读取是任务完成条件。",
        None,
        "query",
    ),
}
CONTROLS = {
    "scat_workflow_catalog": (Empty, "查询已登记的复杂流程、工具组、参数和执行边界。"),
    "scat_workflow_prepare": (
        Prepare,
        "准备有限复杂流程和冻结样本，返回方案等待用户页面确认，不执行模型。",
    ),
    "scat_workflow_list": (Empty, "列出当前会话的复杂流程任务。"),
    "scat_workflow_get": (Get, "查询当前会话流程的步骤、预算、后端和结果。"),
    "scat_workflow_artifact": (Artifact, "读取当前会话流程的标准结果制品。"),
}


def operation_names(proposal: WorkflowProposal):
    names = {"scat_sample_prepare", "scat_report_build", "scat_report_read"}
    if proposal.models:
        names |= {"scat_trial_run", "scat_evaluation_summarize", "scat_evaluation_diagnose"}
    if proposal.materializations:
        names |= {"scat_materialize", "scat_materialization_read"}
    return sorted(names)


def schemas(*, controls=False):
    return [
        {
            "name": name,
            "description": row[1] if controls else row[2],
            "parameters": (row[0] if controls else row[1]).model_json_schema(),
        }
        for name, row in (CONTROLS if controls else OPERATIONS).items()
    ]


def describe():
    from pathlib import Path

    from workflow_runtime.gpu import registration

    gpu = registration(Path(__file__).resolve().parents[2])
    # The union proposal schema describes possible syntax, not runtime admission.
    # Publish per-model constraints so callers need not infer them from that union.
    models = [
        {
            "model_id": "constant-velocity-heading",
            "version": "1",
            "execution": "cpu",
            "parameters": {"window_s": {"type": "number", "minimum": 10, "maximum": 1800}},
        },
        {
            "model_id": "kalman-cv",
            "version": "1",
            "execution": "cpu",
            "parameters": {
                "process_noise_mps2": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 20,
                    "default": 0.5,
                },
                "measurement_noise_m": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 1000,
                    "default": 30,
                },
            },
        },
    ]
    if gpu:
        models.append(
            {
                "model_id": "scat-gbar-v23",
                "version": "23",
                "execution": "gpu",
                "parameters": {},
                "constraints": {
                    "min_observed_s": 350,
                    "max_observation_points": 500,
                    "model_resampled_points": 36,
                    "model_interval_s": 10,
                    "max_horizon_s": 360,
                    "forecast_horizon_multiple_s": 10,
                    "max_cutoffs_per_flight": 4,
                    "max_windows_per_job": gpu.max_windows_per_job,
                    "extra_context_allowed": False,
                },
            }
        )
    return {
        "schema_version": "workflow-catalog/v1",
        "kinds": ["forecast", "comparison", "experiment", "materialization", "report"],
        "protocol": "m6-eval-v1",
        "model_ids": [model["model_id"] for model in models],
        "models": models,
        "catalog_semantics": "Only models/model_ids are registered. proposal_schema enums describe syntax, not availability; parameters are model-specific.",
        "prediction_contract": {
            "version": "point-exact-prediction/v1",
            "forecast": "kind=forecast, observation_last_n required (3..2000); forecast_origin=last_observation. Exact chronological tail; targets are after its final timestamp. No future truth or error metrics.",
            "backtest": "kind=comparison/experiment; forecast_origin=historical_cutoffs (default) or as_of with any timezone-aware cutoff. Observations are timestamp<=as_of; targets are as_of+horizon. Last observation and requested cutoff are recorded separately, with no invented observation at the cutoff. Optional observation_last_n selects exactly N points. Metrics use matched historical truth only; missing truth produces no error metrics.",
            "point_window": "Points are not seconds. Insufficient/invalid/duplicate points reject preparation. Exact N is the entire model observation input, with no window_s truncation; model preprocessing must remain explicit and cannot fetch extra context.",
        },
        "groups": {
            group: [name for name, row in OPERATIONS.items() if row[0] == group]
            for group in sorted({r[0] for r in OPERATIONS.values()})
        },
        "control_group": {"scat_workflows": list(CONTROLS)},
        "proposal_schema": WorkflowProposal.model_json_schema(),
        "production_gpu": gpu is not None,
        "gpu_registration": gpu.pin() if gpu else None,
        "sampling": "seeded selection from at most 100 server candidates, frozen before confirmation",
        "requires_human_http_confirmation": True,
    }
