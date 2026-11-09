"""Deterministic fast path for canonical, high-cost model commands.

This path keeps the same controlled model tools and two-turn confirmation.  It
only avoids making execution availability depend on an external LLM parsing a
fully specified GBAR command.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from agents.experiment_tools import (
    cancel_prediction_experiment_submission,
    submit_prediction_experiment,
)
from agents.materialization_tools import (
    cancel_materialization_job_submission,
    submit_materialization_job,
)
from agents.model_tools import (
    cancel_model_task,
    get_analysis_artifact,
    prepare_model_task,
    submit_model_task,
)
from analysis_runs.artifact_contracts import ARTIFACT_ID_PATTERN
from scat_model_runtime import (
    ComponentReference,
    DatasetKind,
    ModelInputReference,
    ModelTaskRequest,
    ModelTaskType,
)

_BATCH_PATTERN = re.compile(r"scat\d{8}_\d{8}", re.IGNORECASE)
_FLIGHT_PATTERN = re.compile(r"航班(?:编号)?\s*[:：]?\s*(\d{1,20})", re.IGNORECASE)
_FLIGHT_LIST_PATTERN = re.compile(
    r"航班(?:编号)?\s*[:：]?\s*((?:\d{1,20}\s*[,，、]\s*)+\d{1,20})",
    re.IGNORECASE,
)
_LIMIT_PATTERN = re.compile(
    r"(?:最近|读取)\s*(\d{1,5})\s*个(?:源|轨迹)?点",
    re.IGNORECASE,
)
_END_TIME_PATTERN = re.compile(
    r"(?:截止时间|截至|end_time)\s*[:：]?\s*"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))",
    re.IGNORECASE,
)
_CONFIRMATIONS = frozenset({"确认", "确认执行", "同意", "同意执行", "开始执行"})
_CANCELLATIONS = frozenset({"取消", "取消执行", "不执行"})
_EVALUATION_TERMS = ("误差", "评估", "rmse", "mae", "ade", "fde")
_REGRESSION_EVALUATOR = ComponentReference(key="regression-errors", version="1")
_ARTIFACT_ROUTE_PATTERN = re.compile(
    r"/visualizations/(?P<artifact_id>[^/?#\s]+)/3d(?:[/?#\s]|$)",
    re.IGNORECASE,
)
_SEMANTIC_ARTIFACT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._:-])(?P<artifact_id>"
    r"(?:trajectory_prediction|anomaly_classification|model_comparison)-"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{7,190}"
    r")(?![A-Za-z0-9._:-])",
    re.IGNORECASE,
)
_LEGACY_HEX_ID_PATTERN = re.compile(r"(?<![0-9a-f])(?P<artifact_id>[0-9a-f]{32})(?![0-9a-f])")
_ARTIFACT_INTENT_TERMS = (
    "artifact",
    "产物",
    "分析结果",
    "完整结果",
    "恢复结果",
    "展示结果",
)


def _normalized_command(text: str) -> str:
    return text.replace("\\_", "_").strip()


def parse_analysis_artifact_id(text: str) -> str | None:
    """Extract a persisted analysis artifact ID without guessing from flight fields.

    Semantic IDs have a task-type prefix and are therefore unambiguous. A bare
    legacy 32-hex ID is accepted only when the same message explicitly asks for
    an artifact/result; without that intent it could be a run ID.
    """

    normalized = _normalized_command(text)
    route_match = _ARTIFACT_ROUTE_PATTERN.search(normalized)
    if route_match is not None:
        candidate = unquote(route_match.group("artifact_id"))
        if re.fullmatch(ARTIFACT_ID_PATTERN, candidate):
            return candidate

    semantic_match = _SEMANTIC_ARTIFACT_PATTERN.search(normalized)
    if semantic_match is not None:
        candidate = semantic_match.group("artifact_id")
        return candidate if re.fullmatch(ARTIFACT_ID_PATTERN, candidate) else None

    lowered = normalized.lower()
    if any(term in lowered for term in _ARTIFACT_INTENT_TERMS):
        legacy_match = _LEGACY_HEX_ID_PATTERN.search(lowered)
        if legacy_match is not None:
            return legacy_match.group("artifact_id")
    return None


def parse_gbar_prediction_request(text: str) -> ModelTaskRequest | None:
    """Parse only the documented, fully specified GBAR Chinese command form."""
    normalized = _normalized_command(text)
    lowered = normalized.lower()
    evaluation_requested = any(term in lowered for term in _EVALUATION_TERMS)
    if (
        "gbar" not in lowered
        or ("预测" not in normalized and not evaluation_requested)
        or "scat" not in lowered
    ):
        return None
    batch_match = _BATCH_PATTERN.search(normalized)
    flight_match = _FLIGHT_PATTERN.search(normalized)
    flight_list_match = _FLIGHT_LIST_PATTERN.search(normalized)
    limit_match = _LIMIT_PATTERN.search(normalized)
    if batch_match is None or flight_match is None or limit_match is None:
        return None
    limit = int(limit_match.group(1))
    if not 1 <= limit <= 2_000:
        return None
    flight_ids = (
        re.findall(r"\d{1,20}", flight_list_match.group(1))
        if flight_list_match is not None
        else [flight_match.group(1)]
    )
    if len(flight_ids) > 16 or len(set(flight_ids)) != len(flight_ids):
        return None
    end_time_match = _END_TIME_PATTERN.search(normalized)
    try:
        end_time = (
            datetime.fromisoformat(end_time_match.group(1).replace("Z", "+00:00"))
            if end_time_match is not None
            else None
        )
    except ValueError:
        return None
    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch=batch_match.group(0).lower(),
            external_id=flight_id,
            end_time=end_time,
            limit=limit,
        )
        for flight_id in flight_ids
    )
    return ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="scat-gbar-v23",
        model_version="23",
        input_ref=references[0] if len(references) == 1 else None,
        input_refs=references if len(references) > 1 else (),
        parameters={},
        evaluator=_REGRESSION_EVALUATOR,
    )


def _unique_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    values = {
        match.group(1) if match.lastindex else match.group(0) for match in pattern.finditer(text)
    }
    return sorted(values)


def parse_contextual_gbar_prediction_request(
    messages: Sequence[object],
) -> ModelTaskRequest | None:
    """Resolve a GBAR request from the current turn plus one recent SCAT reference.

    The current user turn must explicitly request GBAR prediction. Missing batch,
    flight and source-limit values may be inherited only from the most recent
    human turn that contains one unambiguous SCAT batch and flight reference.
    Explicit values in the current turn always win.
    """

    human_texts = [
        _normalized_command(message.content)
        for message in messages
        if isinstance(message, HumanMessage) and isinstance(message.content, str)
    ]
    if not human_texts:
        return None
    current = human_texts[-1]
    current_lowered = current.lower()
    evaluation_requested = any(term in current_lowered for term in _EVALUATION_TERMS)
    if "gbar" not in current_lowered or ("预测" not in current and not evaluation_requested):
        return None
    direct = parse_gbar_prediction_request(current)
    if direct is not None:
        return direct

    current_batches = _unique_matches(_BATCH_PATTERN, current)
    current_flights = _unique_matches(_FLIGHT_PATTERN, current)
    current_limits = _unique_matches(_LIMIT_PATTERN, current)
    current_end_times = _unique_matches(_END_TIME_PATTERN, current)
    if any(
        len(values) > 1
        for values in (current_batches, current_flights, current_limits, current_end_times)
    ):
        return None

    for previous in reversed(human_texts[:-1]):
        previous_batches = _unique_matches(_BATCH_PATTERN, previous)
        previous_flights = _unique_matches(_FLIGHT_PATTERN, previous)
        if not previous_batches and not previous_flights:
            continue
        if len(previous_batches) != 1 or len(previous_flights) != 1:
            return None
        previous_limits = _unique_matches(_LIMIT_PATTERN, previous)
        previous_end_times = _unique_matches(_END_TIME_PATTERN, previous)
        if len(previous_limits) > 1 or len(previous_end_times) > 1:
            return None
        batch = current_batches[0] if current_batches else previous_batches[0]
        flight = current_flights[0] if current_flights else previous_flights[0]
        limit_text = (
            current_limits[0]
            if current_limits
            else previous_limits[0]
            if previous_limits
            else "500"
        )
        limit = int(limit_text)
        if not 1 <= limit <= 2_000:
            return None
        end_time_text = (
            current_end_times[0]
            if current_end_times
            else previous_end_times[0]
            if previous_end_times
            else None
        )
        try:
            end_time = (
                datetime.fromisoformat(end_time_text.replace("Z", "+00:00"))
                if end_time_text is not None
                else None
            )
        except ValueError:
            return None
        return ModelTaskRequest(
            task_type=ModelTaskType.TRAJECTORY_PREDICTION,
            model_key="scat-gbar-v23",
            model_version="23",
            input_ref=ModelInputReference(
                dataset=DatasetKind.SCAT,
                batch=batch.lower(),
                external_id=flight,
                end_time=end_time,
                limit=limit,
            ),
            parameters={},
            evaluator=_REGRESSION_EVALUATOR,
        )
    return None


def is_model_confirmation(text: str) -> bool:
    normalized = _normalized_command(text).rstrip("。.!！ ")
    return normalized in _CONFIRMATIONS


def is_model_cancellation(text: str) -> bool:
    normalized = _normalized_command(text).rstrip("。.!！ ")
    return normalized in _CANCELLATIONS


def _tool_messages(
    *, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]
) -> list[AIMessage | ToolMessage]:
    call_id = f"deterministic-{uuid4().hex}"
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool_name,
                    "args": arguments,
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            tool_call_id=call_id,
            name=tool_name,
        ),
    ]


def _last_human_text(state: Mapping[str, Any]) -> str | None:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            return message.content
    return None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def handle_deterministic_model_command(
    state: Mapping[str, Any],
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """Handle canonical GBAR prepare/confirm commands without calling an LLM."""
    text = _last_human_text(state)
    if text is None:
        return {"deterministic_command_handled": False}

    artifact_id = parse_analysis_artifact_id(text)
    if artifact_id is not None:
        result = await get_analysis_artifact.ainvoke(
            {"artifact_id": artifact_id},
            config=config,
        )
        messages = _tool_messages(
            tool_name="get_analysis_artifact",
            arguments={"artifact_id": artifact_id},
            result=result,
        )
        if result.get("status") == "ok":
            messages.append(
                AIMessage(
                    content=(
                        "已恢复完整分析结果。下方可直接查看结构化指标、对比表和二维轨迹；"
                        "需要同步回放或三维视图时，可点击结果中的可视化按钮。"
                    )
                )
            )
        else:
            messages.append(
                AIMessage(
                    content=str(
                        result.get("message")
                        or "未能恢复该分析结果，请确认 artifact ID 和当前用户权限。"
                    )
                )
            )
        return {
            "deterministic_command_handled": True,
            "messages": messages,
        }

    pending_action = state.get("pending_action")
    if isinstance(pending_action, Mapping):
        pending_confirmation_id = pending_action.get("confirmation_id")
        pending_domain = pending_action.get("domain")
        if isinstance(pending_confirmation_id, str) and pending_domain in {
            "model",
            "materialization",
            "experiment",
        }:
            if is_model_cancellation(text):
                if pending_domain == "model":
                    cancel_tool = cancel_model_task
                elif pending_domain == "materialization":
                    cancel_tool = cancel_materialization_job_submission
                else:
                    cancel_tool = cancel_prediction_experiment_submission
                result = await cancel_tool.ainvoke(
                    {"confirmation_id": pending_confirmation_id},
                    config=config,
                )
                messages = _tool_messages(
                    tool_name=cancel_tool.name,
                    arguments={"confirmation_id": pending_confirmation_id},
                    result=result,
                )
                messages.append(
                    AIMessage(content=str(result.get("message") or "待执行操作已取消。"))
                )
                return {
                    "deterministic_command_handled": True,
                    "pending_action": None,
                    "pending_model_confirmation_id": None,
                    "messages": messages,
                }
            if is_model_confirmation(text):
                if pending_domain == "model":
                    submit_tool = submit_model_task
                elif pending_domain == "materialization":
                    submit_tool = submit_materialization_job
                else:
                    submit_tool = submit_prediction_experiment
                result = await submit_tool.ainvoke(
                    {"confirmation_id": pending_confirmation_id},
                    config=config,
                )
                messages = _tool_messages(
                    tool_name=submit_tool.name,
                    arguments={"confirmation_id": pending_confirmation_id},
                    result=result,
                )
                status = str(result.get("status") or "")
                data = _mapping(result.get("data"))
                if status == "queued":
                    identifier = (
                        data.get("run_id") or data.get("job_id") or data.get("experiment_id")
                    )
                    suffix = f"编号：{identifier}。" if identifier else ""
                    message = str(result.get("message") or f"任务已入队。{suffix}")
                elif status == "succeeded":
                    message = str(result.get("message") or "任务执行完成。")
                else:
                    message = str(result.get("message") or "任务提交失败。")
                messages.append(AIMessage(content=message))
                return {
                    "deterministic_command_handled": True,
                    "pending_action": None,
                    "pending_model_confirmation_id": None,
                    "messages": messages,
                }

    confirmation_id = state.get("pending_model_confirmation_id")
    if isinstance(confirmation_id, str) and is_model_cancellation(text):
        result = await cancel_model_task.ainvoke(
            {"confirmation_id": confirmation_id},
            config=config,
        )
        message = str(result.get("message") or "模型任务已取消。")
        messages = _tool_messages(
            tool_name="cancel_model_task",
            arguments={"confirmation_id": confirmation_id},
            result=result,
        )
        messages.append(AIMessage(content=message))
        return {
            "deterministic_command_handled": True,
            "pending_model_confirmation_id": None,
            "messages": messages,
        }

    if isinstance(confirmation_id, str) and is_model_confirmation(text):
        result = await submit_model_task.ainvoke(
            {"confirmation_id": confirmation_id},
            config=config,
        )
        messages = _tool_messages(
            tool_name="submit_model_task",
            arguments={"confirmation_id": confirmation_id},
            result=result,
        )
        if result.get("status") == "succeeded":
            data = _mapping(result.get("data"))
            summary = _mapping(data.get("result_summary"))
            error_analysis = _mapping(summary.get("error_analysis"))
            error_suffix = (
                f"误差分析已完成，ADE {error_analysis.get('ade_metres')} m，"
                f"FDE {error_analysis.get('fde_metres')} m；"
                if error_analysis
                else ""
            )
            messages.append(
                AIMessage(
                    content=(
                        "GBAR v23 预测完成。"
                        f"共处理 {summary.get('batch_size', 0)} 条轨迹，生成 "
                        f"{summary.get('point_count', 0)} 个未来轨迹点，"
                        f"预测时域 {summary.get('forecast_horizon_seconds', 0)} 秒；"
                        f"{error_suffix}"
                        "下方为六特征预测结果和二维轨迹。"
                    )
                )
            )
        elif result.get("status") == "queued":
            data = _mapping(result.get("data"))
            messages.append(
                AIMessage(
                    content=(
                        "GBAR v23 预测任务已进入后台执行，页面可以继续使用。"
                        f"运行编号：{data.get('run_id')}。"
                        "稍后可让我查询该运行编号；如需停止，请明确要求取消该运行。"
                    )
                )
            )
        else:
            messages.append(
                AIMessage(content=str(result.get("message") or "GBAR 预测任务执行失败。"))
            )
        return {
            "deterministic_command_handled": True,
            "pending_model_confirmation_id": None,
            "messages": messages,
        }

    raw_messages = state.get("messages", [])
    context_messages = raw_messages if isinstance(raw_messages, Sequence) else []
    request = parse_contextual_gbar_prediction_request(context_messages)
    if request is None:
        return {"deterministic_command_handled": False}
    if isinstance(confirmation_id, str):
        await cancel_model_task.ainvoke(
            {"confirmation_id": confirmation_id},
            config=config,
        )
    result = await prepare_model_task.ainvoke(
        {"request": request},
        config=config,
    )
    output_messages = _tool_messages(
        tool_name="prepare_model_task",
        arguments={"request": request.model_dump(mode="json", exclude_none=True)},
        result=result,
    )
    if result.get("status") != "awaiting_confirmation":
        output_messages.append(
            AIMessage(content=str(result.get("message") or "GBAR 预测任务准备失败。"))
        )
        return {
            "deterministic_command_handled": True,
            "pending_model_confirmation_id": None,
            "messages": output_messages,
        }
    data = _mapping(result.get("data"))
    issued_confirmation_id = data.get("confirmation_id")
    if not isinstance(issued_confirmation_id, str):
        output_messages.append(AIMessage(content="GBAR 预测任务准备失败：没有生成确认凭据。"))
        return {
            "deterministic_command_handled": True,
            "pending_model_confirmation_id": None,
            "messages": output_messages,
        }
    input_ref = request.primary_input_ref
    flight_ids = ", ".join(reference.external_id for reference in request.references)
    end_time_line = (
        f"- 历史截止时间：{input_ref.end_time.isoformat()}\n"
        if input_ref.end_time is not None
        else ""
    )
    evaluator_line = (
        "- 误差分析：各特征 RMSE/MAE、轨迹 ADE/FDE\n" if request.evaluator is not None else ""
    )
    evaluation_window_line = (
        "- 评估窗口：按指定历史截止时间读取后续真值\n"
        if input_ref.end_time is not None
        else "- 评估窗口：自动保留轨迹末端真值（默认回测）\n"
    )
    output_messages.append(
        AIMessage(
            content=(
                "GBAR v23 轨迹预测任务已准备，但尚未执行。\n\n"
                f"- 模型：scat-gbar-v23:23\n"
                f"- SCAT 批次：{input_ref.batch}\n"
                f"- 航班 external_id：{flight_ids}\n"
                f"- 批量大小：{len(request.references)}\n"
                f"- 读取最近源点：{input_ref.limit}\n"
                f"{end_time_line}"
                f"{evaluator_line}"
                f"{evaluation_window_line}"
                "- 运行参数：无\n\n"
                "如确认上述参数，请在下一条消息回复“确认执行”；回复“取消执行”可取消。"
            )
        )
    )
    return {
        "deterministic_command_handled": True,
        "pending_action": {
            "domain": "model",
            "action_kind": "gbar_prediction",
            "confirmation_id": issued_confirmation_id,
            "expires_at": data.get("expires_at"),
            "summary": data.get("request_summary") or {},
        },
        "pending_model_confirmation_id": issued_confirmation_id,
        "messages": output_messages,
    }


__all__ = [
    "handle_deterministic_model_command",
    "is_model_cancellation",
    "is_model_confirmation",
    "parse_analysis_artifact_id",
    "parse_contextual_gbar_prediction_request",
    "parse_gbar_prediction_request",
]
