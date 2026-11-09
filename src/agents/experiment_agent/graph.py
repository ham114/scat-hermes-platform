"""M6 内部实验子代理：自研预测模型实验的确定性编排（M6-08 起接入）。

按《Agent 拓扑与子代理接入规范》（计划 §5.3）建立：

- 本包**不对外暴露**：在 `agents.agents` 注册为 `experiment-agent`（`expose=False`），
  不出现在 `/info`，也不面向用户直接对话；由 `scat-flight-agent` 的边界工具
  （`agents.experiment_tools`）在进程内调用；
- 本轮职责（`predictor_evaluation`）：校验请求 → 解析预测器（注册表）→ 落一条
  `pending` 实验记录（`scat_model_experiments`）；**不执行批量计算**——执行由
  确定性执行器领取（D-19 模式：`python -m scat.experiments.cli claim`）；
- 训练、模型对比等后续实验种类随 M6 推进在此扩展节点；
- `ExperimentAgentState` 中的运行期 payload 字段绝不返回给 LLM；对外只经
  `public_experiment_agent_result()` 投影。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from scat.experiments.evaluation import EvaluationConfig
from scat.experiments.repository import ExperimentRepository
from scat.experiments.runner import create_pending_experiment
from scat.infrastructure.database import flight_database
from scat.prediction.alignment import HORIZONS_S

SUPPORTED_EXPERIMENT_KINDS = ("predictor_evaluation",)
DEFAULT_STRIDE_S = 60.0
DEFAULT_MAX_CUTOFFS_PER_FLIGHT = 12

ExperimentAgentStatus = Literal["running", "queued", "failed"]


class ExperimentAgentState(TypedDict, total=False):
    """内部图状态；运行期 payload 绝不能原样返回给 LLM。

    这里使用普通 TypedDict 而不是 MessagesState，因为该内部图是确定性业务
    Workflow，不需要保存对话消息。``total=False`` 允许每个节点只写自己负责
    的局部字段。
    """

    request: Mapping[str, Any]
    requested_by: str | None
    run_id: str
    status: ExperimentAgentStatus
    progress: float
    result_summary: dict[str, Any]
    error_code: str
    message: str


def _failed(*, code: str, message: str) -> ExperimentAgentState:
    return {
        "status": "failed",
        "progress": 1.0,
        "error_code": code,
        "message": message,
    }


def initialize(state: ExperimentAgentState) -> ExperimentAgentState:
    """为本次实验分配稳定 run_id（实验台账 experiment_id 由 persist_pending 落地）。"""
    return {
        "run_id": state.get("run_id") or uuid4().hex,
        "status": "running",
        "progress": 0.0,
    }


def validate_request(state: ExperimentAgentState) -> ExperimentAgentState:
    """校验实验请求的最小字段与种类白名单。"""
    request = state.get("request")
    if not isinstance(request, Mapping):
        return _failed(code="invalid_request", message="experiment request 缺失或格式不正确。")
    experiment_kind = request.get("experiment_kind")
    if not isinstance(experiment_kind, str) or not experiment_kind.strip():
        return _failed(code="invalid_request", message="必须提供非空的 experiment_kind。")
    if experiment_kind not in SUPPORTED_EXPERIMENT_KINDS:
        return _failed(
            code="unsupported_experiment_kind",
            message=(
                f"不支持的实验种类：{experiment_kind!r}；"
                f"当前支持 {', '.join(SUPPORTED_EXPERIMENT_KINDS)}。"
            ),
        )
    batch = request.get("batch")
    if not isinstance(batch, str) or not batch.strip():
        return _failed(code="invalid_request", message="必须提供非空的 batch。")
    predictor_id = request.get("predictor_id")
    if not isinstance(predictor_id, str) or not predictor_id.strip():
        return _failed(code="invalid_request", message="必须提供非空的 predictor_id。")
    limit = request.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        return _failed(code="invalid_request", message="limit 必须为正整数或 null。")
    return {}


def _route_after_validate(state: ExperimentAgentState) -> str:
    # 路由函数只读取验证节点写入的 error_code，不承担状态修改；这样验证逻辑
    # 和控制流可以分别单测。
    return "failed" if state.get("error_code") else "persist_pending"


async def persist_pending(state: ExperimentAgentState) -> ExperimentAgentState:
    """落一条 pending 实验记录（不执行计算）；未知预测器转为稳定错误码。"""

    # 对话请求只负责创建可审计的 pending 台账。高成本批计算由独立执行器
    # 领取，避免把长任务生命周期绑在一次 Agent 调用上。
    request = state["request"]
    config = EvaluationConfig(
        batch=str(request["batch"]).strip(),
        predictor_id=str(request["predictor_id"]).strip(),
        horizons_s=tuple(float(value) for value in (request.get("horizons_s") or HORIZONS_S)),
        stride_s=float(request.get("stride_s", DEFAULT_STRIDE_S)),
        max_cutoffs_per_flight=int(
            request.get("max_cutoffs_per_flight", DEFAULT_MAX_CUTOFFS_PER_FLIGHT)
        ),
    )
    limit = request.get("limit")
    requested_by = state.get("requested_by")
    try:
        record = await create_pending_experiment(
            config=config,
            experiment_repository=ExperimentRepository(flight_database.db),
            limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
            requested_by=requested_by if isinstance(requested_by, str) else None,
            source="chat",
        )
    except (KeyError, ValueError) as error:
        return _failed(code="unknown_predictor", message=f"未知预测器：{error}")
    return {
        "status": "queued",
        "progress": 1.0,
        "result_summary": {
            "experiment_id": record.experiment_id,
            "kind": record.kind,
            "batch": record.batch,
            "predictor_id": record.predictor_id,
            "predictor_version": record.predictor_version,
            "protocol_version": record.protocol_version,
            "config": record.config,
            "record_status": record.status,
            "created_at": record.created_at.isoformat(),
        },
    }


def build_experiment_agent() -> CompiledStateGraph:
    """构建并编译内部实验图。

    图的主路径是 initialize → validate_request → persist_pending；验证失败通过
    条件边直接结束。这里不注入 checkpointer，部署时可由父图或服务层决定
    持久化策略。
    """

    builder = StateGraph(ExperimentAgentState)
    builder.add_node("initialize", initialize)
    builder.add_node("validate_request", validate_request)
    builder.add_node("persist_pending", persist_pending)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "validate_request")
    builder.add_conditional_edges(
        "validate_request",
        _route_after_validate,
        {"failed": END, "persist_pending": "persist_pending"},
    )
    builder.add_edge("persist_pending", END)
    return builder.compile()


experiment_agent = build_experiment_agent()


def public_experiment_agent_result(state: ExperimentAgentState) -> dict[str, Any]:
    """对外投影：只暴露稳定字段，内部 payload 与原始请求不回传。"""

    # “内部 State”和“公共响应”分离是安全边界：即使以后 State 增加数据库
    # 对象、调试字段或原始请求，也不会因为直接序列化整个 State 而泄露。
    return {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "progress": state.get("progress"),
        "message": state.get("message"),
        "error_code": state.get("error_code"),
        "result_summary": state.get("result_summary"),
    }
