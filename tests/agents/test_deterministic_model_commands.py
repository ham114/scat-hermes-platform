"""确定性模型命令的解析、确认、取消和结果恢复测试。

这些测试证明高成本控制命令不依赖 LLM：规范请求可直接转为类型化工具调用，
而 State 只保留不透明确认句柄，避免用户在第二轮修改已准备的完整参数。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from agents import deterministic_model_commands as module
from agents.deterministic_model_commands import (
    is_model_cancellation,
    is_model_confirmation,
    parse_analysis_artifact_id,
    parse_contextual_gbar_prediction_request,
    parse_gbar_prediction_request,
)

SEMANTIC_ARTIFACT_ID = (
    "model_comparison-scat-comparison-v1-scat-scat20161015_20161021-"
    "104363-h360-n1-20260921T195627-f82a57"
)


# 结果恢复首先验证 artifact 标识解析，防止把普通航班号或 run_id 误当成
# 可读取的分析产物。
def test_parse_analysis_artifact_id_from_text_or_visualization_url() -> None:
    assert (
        parse_analysis_artifact_id(f"请在对话中恢复完整分析结果：`{SEMANTIC_ARTIFACT_ID}`")
        == SEMANTIC_ARTIFACT_ID
    )
    assert (
        parse_analysis_artifact_id(
            f"http://localhost:5173/visualizations/{SEMANTIC_ARTIFACT_ID}/3d?thread_id=t1"
        )
        == SEMANTIC_ARTIFACT_ID
    )
    assert (
        parse_analysis_artifact_id(SEMANTIC_ARTIFACT_ID.replace("_", r"\_")) == SEMANTIC_ARTIFACT_ID
    )


def test_parse_analysis_artifact_id_does_not_treat_bare_run_id_as_artifact() -> None:
    assert parse_analysis_artifact_id("查看运行编号 " + "a" * 32) is None
    assert parse_analysis_artifact_id("查看航班 104363") is None


def test_parse_legacy_artifact_id_requires_explicit_result_intent() -> None:
    legacy_id = "b" * 32
    assert parse_analysis_artifact_id(f"恢复 artifact_id={legacy_id}") == legacy_id
    assert parse_analysis_artifact_id(f"请展示完整分析结果 {legacy_id}") == legacy_id
    assert parse_analysis_artifact_id(legacy_id) is None


@pytest.mark.asyncio
async def test_semantic_artifact_id_restores_result_without_llm(monkeypatch) -> None:
    restored = AsyncMock(
        return_value={
            "status": "ok",
            "operation": "get_analysis_artifact",
            "message": "完整分析结果恢复成功。",
            "data": {"artifact_id": SEMANTIC_ARTIFACT_ID},
            "analysis_result": {
                "schema_version": "1.6",
                "dataset": "scat",
                "operation": "run_model_comparison",
                "title": "统一预测对比结果",
                "context": {},
                "blocks": [],
            },
        }
    )
    monkeypatch.setattr(
        module,
        "get_analysis_artifact",
        SimpleNamespace(ainvoke=restored),
    )
    config = RunnableConfig(configurable={"thread_id": "thread-1", "user_id": "user-1"})

    update = await module.handle_deterministic_model_command(
        {"messages": [HumanMessage(content=f"请在对话中展示 `{SEMANTIC_ARTIFACT_ID}`")]},
        config,
    )

    assert update["deterministic_command_handled"] is True
    assert update["messages"][0].tool_calls[0]["name"] == "get_analysis_artifact"
    assert "已恢复完整分析结果" in update["messages"][-1].content
    restored.assert_awaited_once_with({"artifact_id": SEMANTIC_ARTIFACT_ID}, config=config)


def test_parse_canonical_gbar_request() -> None:
    request = parse_gbar_prediction_request(
        "请用 GBAR v23 预测 SCAT 批次 scat20161015_20161021 中航班 100002 的未来轨迹，读取最近 300 个源点。"
    )

    assert request is not None
    assert request.model_key == "scat-gbar-v23"
    assert request.model_version == "23"
    assert request.input_ref.batch == "scat20161015_20161021"
    assert request.input_ref.external_id == "100002"
    assert request.input_ref.limit == 300
    assert request.evaluator is not None
    assert request.evaluator.registry_id == "regression-errors:1"


def test_parse_accepts_markdown_escaped_batch_underscore() -> None:
    request = parse_gbar_prediction_request(
        r"用GBAR预测SCAT批次scat20161015\_20161021中航班100002，最近300个源点"
    )

    assert request is not None
    assert request.input_ref.batch == "scat20161015_20161021"


def test_parse_accepts_one_bounded_batch_of_flights() -> None:
    request = parse_gbar_prediction_request(
        "用GBAR预测SCAT批次scat20161015_20161021中航班100002、100003、100004，最近300个源点"
    )

    assert request is not None
    assert request.input_ref is None
    assert [item.external_id for item in request.input_refs] == [
        "100002",
        "100003",
        "100004",
    ]


def test_parse_keeps_historical_forecast_end_time() -> None:
    request = parse_gbar_prediction_request(
        "用 GBAR 预测 SCAT 批次 scat20161015_20161021 中航班 100002，"
        "最近 300 个源点，截至 2016-10-20T20:08:00Z"
    )

    assert request is not None
    assert request.primary_input_ref.end_time == datetime(2016, 10, 20, 20, 8, tzinfo=UTC)
    assert request.evaluator is not None
    assert request.evaluator.registry_id == "regression-errors:1"


def test_parse_gbar_error_analysis_requires_and_registers_historical_evaluator() -> None:
    request = parse_gbar_prediction_request(
        "用 GBAR 预测并做误差分析：SCAT 批次 scat20161015_20161021 中航班 100002，"
        "最近 300 个源点，截至 2016-10-20T20:08:00Z"
    )

    assert request is not None
    assert request.evaluator is not None
    assert request.evaluator.registry_id == "regression-errors:1"
    assert request.primary_input_ref.end_time is not None


def test_parse_gbar_error_analysis_without_cutoff_uses_default_holdout() -> None:
    request = parse_gbar_prediction_request(
        "用 GBAR 预测并做误差分析：SCAT 批次 scat20161015_20161021 中航班 100002，最近 300 个源点"
    )

    assert request is not None
    assert request.primary_input_ref.end_time is None
    assert request.evaluator is not None
    assert request.evaluator.registry_id == "regression-errors:1"


def test_parse_rejects_incomplete_or_unbounded_commands() -> None:
    assert parse_gbar_prediction_request("预测航班 100002") is None
    assert (
        parse_gbar_prediction_request(
            "用GBAR预测SCAT批次scat20161015_20161021中航班100002，最近9999个源点"
        )
        is None
    )


def test_confirmation_and_cancellation_are_explicit() -> None:
    assert is_model_confirmation("确认执行。")
    assert is_model_cancellation("取消执行")
    assert not is_model_confirmation("随便执行吧")


def test_contextual_request_inherits_recent_scat_reference_and_limit() -> None:
    request = parse_contextual_gbar_prediction_request(
        [
            HumanMessage(
                content=("查询 SCAT 批次 scat20161015_20161021 中航班 100012 最近 300 个轨迹点")
            ),
            AIMessage(content="已返回航班轨迹。"),
            HumanMessage(content="使用 GBAR 模型进行预测"),
        ]
    )

    assert request is not None
    assert request.input_ref.batch == "scat20161015_20161021"
    assert request.input_ref.external_id == "100012"
    assert request.input_ref.limit == 300
    assert request.evaluator is not None


def test_contextual_request_prefers_explicit_current_overrides() -> None:
    request = parse_contextual_gbar_prediction_request(
        [
            HumanMessage(
                content=("查询 SCAT 批次 scat20161015_20161021 中航班 100012 最近 300 个轨迹点")
            ),
            HumanMessage(content="用 GBAR 预测航班 100013，最近 500 个源点"),
        ]
    )

    assert request is not None
    assert request.input_ref.batch == "scat20161015_20161021"
    assert request.input_ref.external_id == "100013"
    assert request.input_ref.limit == 500


def test_contextual_request_does_not_guess_without_unique_reference() -> None:
    assert (
        parse_contextual_gbar_prediction_request(
            [
                HumanMessage(content="查询 SCAT 批次 scat20161015_20161021"),
                HumanMessage(content="使用 GBAR 模型进行预测"),
            ]
        )
        is None
    )
    assert (
        parse_contextual_gbar_prediction_request(
            [
                HumanMessage(
                    content=(
                        "查询 SCAT 批次 scat20161015_20161021 中航班 100012，最近 300 个轨迹点"
                    )
                ),
                HumanMessage(content="比较航班 100013 和航班 100014"),
                HumanMessage(content="使用 GBAR 模型进行预测"),
            ]
        )
        is None
    )


@pytest.mark.asyncio
async def test_prepared_state_keeps_only_opaque_confirmation_id(monkeypatch) -> None:
    """prepare 后 State 不得保存可被下一轮对话修改的原始执行请求。"""

    prepare = AsyncMock(
        return_value={
            "status": "awaiting_confirmation",
            "data": {"confirmation_id": "a" * 32},
        }
    )
    monkeypatch.setattr(
        module,
        "prepare_model_task",
        SimpleNamespace(ainvoke=prepare),
    )
    config = RunnableConfig(configurable={"thread_id": "thread-1", "user_id": "user-1"})

    update = await module.handle_deterministic_model_command(
        {
            "messages": [
                HumanMessage(
                    content=("用GBAR预测SCAT批次scat20161015_20161021中航班100002，最近300个源点")
                )
            ]
        },
        config,
    )

    assert update["pending_model_confirmation_id"] == "a" * 32
    assert "pending_model_request" not in update
    assert prepare.await_args.kwargs["config"] == config


@pytest.mark.asyncio
async def test_cancellation_revokes_server_side_confirmation(monkeypatch) -> None:
    """取消必须调用服务端撤销，而不只是清空客户端/图内字段。"""

    cancel = AsyncMock(
        return_value={
            "status": "cancelled",
            "message": "模型任务已取消，确认码已撤销。",
        }
    )
    monkeypatch.setattr(
        module,
        "cancel_model_task",
        SimpleNamespace(ainvoke=cancel),
    )
    config = RunnableConfig(configurable={"thread_id": "thread-1", "user_id": "user-1"})

    update = await module.handle_deterministic_model_command(
        {
            "messages": [HumanMessage(content="取消执行")],
            "pending_model_confirmation_id": "b" * 32,
        },
        config,
    )

    assert update["pending_model_confirmation_id"] is None
    assert update["messages"][-1].content == "模型任务已取消，确认码已撤销。"
    assert cancel.await_args.kwargs["config"] == config


@pytest.mark.asyncio
async def test_confirmation_reports_asynchronous_run_id(monkeypatch) -> None:
    """确认异步任务后向用户返回可追踪的 run_id，并清空待确认状态。"""

    submit = AsyncMock(
        return_value={
            "status": "queued",
            "message": "模型任务已进入后台队列。",
            "data": {"run_id": "c" * 32},
        }
    )
    monkeypatch.setattr(
        module,
        "submit_model_task",
        SimpleNamespace(ainvoke=submit),
    )

    update = await module.handle_deterministic_model_command(
        {
            "messages": [HumanMessage(content="确认执行")],
            "pending_model_confirmation_id": "b" * 32,
        },
        RunnableConfig(configurable={"thread_id": "thread-1", "user_id": "user-1"}),
    )

    assert update["pending_model_confirmation_id"] is None
    assert "后台执行" in update["messages"][-1].content
    assert "c" * 32 in update["messages"][-1].content


@pytest.mark.asyncio
async def test_unified_confirmation_dispatches_materialization_without_llm(monkeypatch) -> None:
    """公共 pending_action 应按 domain 确定性分发到正确提交工具。"""

    submit = AsyncMock(
        return_value={
            "status": "queued",
            "message": "物化任务已入队。",
            "data": {"job_id": "job-1"},
        }
    )
    monkeypatch.setattr(
        module,
        "submit_materialization_job",
        SimpleNamespace(name="submit_materialization_job", ainvoke=submit),
    )
    config = RunnableConfig(configurable={"thread_id": "thread-1", "user_id": "user-1"})

    update = await module.handle_deterministic_model_command(
        {
            "messages": [HumanMessage(content="确认执行")],
            "pending_action": {
                "domain": "materialization",
                "action_kind": "materialization_job",
                "confirmation_id": "d" * 32,
                "expires_at": None,
                "summary": {},
            },
        },
        config,
    )

    assert update["deterministic_command_handled"] is True
    assert update["pending_action"] is None
    assert update["messages"][-1].content == "物化任务已入队。"
    submit.assert_awaited_once_with({"confirmation_id": "d" * 32}, config=config)


@pytest.mark.asyncio
async def test_unified_cancellation_dispatches_experiment_without_llm(monkeypatch) -> None:
    cancel = AsyncMock(return_value={"status": "cancelled", "message": "预测实验准备已取消。"})
    monkeypatch.setattr(
        module,
        "cancel_prediction_experiment_submission",
        SimpleNamespace(name="cancel_prediction_experiment_submission", ainvoke=cancel),
    )
    config = RunnableConfig(configurable={"thread_id": "thread-1", "user_id": "user-1"})

    update = await module.handle_deterministic_model_command(
        {
            "messages": [HumanMessage(content="取消执行")],
            "pending_action": {
                "domain": "experiment",
                "action_kind": "prediction_experiment",
                "confirmation_id": "e" * 32,
                "expires_at": None,
                "summary": {},
            },
        },
        config,
    )

    assert update["pending_action"] is None
    assert update["messages"][-1].content == "预测实验准备已取消。"
    cancel.assert_awaited_once_with({"confirmation_id": "e" * 32}, config=config)
