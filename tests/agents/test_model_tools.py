"""受控模型任务工具的契约、安全边界和生命周期测试。

重点覆盖 prepare/submit/cancel、一次性确认码、thread/user 作用域、owner 隔离、
同步与异步执行以及 artifact 恢复。这里测试的是服务端强制约束，而不是 prompt
是否“要求模型守规矩”。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pymongo.errors import ConnectionFailure

pytest.importorskip("langchain_core")

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from agents import model_tools as module
from agents.scat_agent import (
    AVIATION_AGENT_INSTRUCTIONS,
    aviation_tools,
    capture_pending_action,
    select_aviation_tools,
)
from scat_model_runtime import (
    COMPARISON_TASK_INPUT_SCHEMA,
    COMPARISON_TASK_OUTPUT_SCHEMA,
    ComparisonMethodResult,
    ComparisonMethodSummary,
    ComparisonMetricDistribution,
    ComparisonSampleResult,
    ComparisonTaskOutput,
    ComparisonTrajectoryPoint,
    ComponentReference,
    DatasetKind,
    ModelAvailability,
    ModelExecutionMode,
    ModelInputReference,
    ModelRegistry,
    ModelResourceLimits,
    ModelRunStatus,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
    RegressionEvaluation,
    RegressionPredictionSeries,
    RegressionTargetError,
    RegressionTaskOutput,
)


@pytest.fixture(autouse=True)
def _isolated_analysis_run_repository(monkeypatch):
    """Model tool unit tests must not write to the developer's MongoDB."""

    # 所有测试自动替换业务 Repository/Executor，既避免污染开发者数据库，也让
    # 断言聚焦于工具传递的 owner、状态和调用次数。
    artifact_repository = SimpleNamespace(
        create=AsyncMock(),
        get_for_owner=AsyncMock(return_value=None),
        list_for_owner=AsyncMock(return_value=[]),
    )
    repository = SimpleNamespace(
        create=AsyncMock(),
        transition=AsyncMock(),
        get_for_owner=AsyncMock(return_value=None),
        list_for_owner=AsyncMock(return_value=[]),
        artifact_repository=artifact_repository,
    )
    executor = SimpleNamespace(
        submit=AsyncMock(),
        cancel=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(module, "analysis_run_repository", repository)
    monkeypatch.setattr(module, "analysis_artifact_repository", artifact_repository)
    monkeypatch.setattr(module, "analysis_run_executor", executor)
    repository.run_executor = executor
    return repository


def _spec(
    execution_mode: ModelExecutionMode = ModelExecutionMode.SYNC,
) -> ModelSpec:
    return ModelSpec(
        model_key="test-model",
        version="1.0.0",
        display_name="Test model",
        description="Test-only model metadata.",
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        supported_datasets={DatasetKind.UAV},
        input_schema="test-features/v1",
        output_schema="test-prediction/v1",
        execution_mode=execution_mode,
        resource_limits=ModelResourceLimits(
            max_input_points=100,
            max_output_points=10,
            timeout_seconds=5,
        ),
        availability=ModelAvailability.AVAILABLE,
        is_default=True,
    )


def _comparison_spec() -> ModelSpec:
    return ModelSpec(
        model_key="scat-comparison-v1",
        version="1",
        display_name="SCAT comparison",
        description="Test comparison registration.",
        task_type=ModelTaskType.MODEL_COMPARISON,
        supported_datasets={DatasetKind.SCAT},
        input_schema=COMPARISON_TASK_INPUT_SCHEMA,
        output_schema=COMPARISON_TASK_OUTPUT_SCHEMA,
        execution_mode=ModelExecutionMode.ASYNC,
        resource_limits=ModelResourceLimits(
            min_input_points=71,
            max_input_points=2_000,
            max_output_points=144,
            timeout_seconds=3_600,
            max_batch_size=50,
        ),
        availability=ModelAvailability.AVAILABLE,
    )


def _request(*, confirmed: bool = False, threshold: float = 0.5) -> ModelTaskRequest:
    return ModelTaskRequest(
        task_type=ModelTaskType.ANOMALY_CLASSIFICATION,
        model_key="test-model",
        input_ref=ModelInputReference(
            dataset=DatasetKind.UAV,
            external_id="flight-1",
            limit=2,
            channels=("cmp_pitch",),
        ),
        parameters={"threshold": threshold},
        confirmed=confirmed,
    )


def _config(thread_id: str, user_id: str = "user-1") -> RunnableConfig:
    """构造与生产工具一致的确认作用域。"""

    return RunnableConfig(configurable={"thread_id": thread_id, "user_id": user_id})


def test_shared_random_criteria_supports_non_a320_and_rejects_conflicts():
    criteria = module.PrepareRandomGbarBatchInput(
        sample_size=3,
        exclude_aircraft_types=["a320"],
        adeps=["essa", "ekch"],
        min_track_points=400,
    )

    assert criteria.exclude_aircraft_types == ("A320",)
    assert criteria.adeps == ("ESSA", "EKCH")
    with pytest.raises(ValidationError, match="精确匹配和排除"):
        module.PrepareRandomGbarBatchInput(
            sample_size=3,
            aircraft_type="A320",
            exclude_aircraft_types=["A320"],
        )

    comparison = module.PrepareRandomComparisonBatchInput(
        batch=r"scat20161015\_20161021",
        sample_size=6,
        cutoffs_per_flight=1,
    )
    assert comparison.batch == "scat20161015_20161021"
    assert comparison.has_fpl is True
    assert comparison.has_scat_prediction is True
    with pytest.raises(ValidationError, match="Input should be True"):
        module.PrepareRandomComparisonBatchInput(
            sample_size=6,
            has_scat_prediction=False,
        )


def test_comparison_schema_normalizes_explicit_flights_and_rejects_duplicates():
    request = module.PrepareComparisonBatchInput(
        batch=r"scat20161015\_20161021",
        external_ids=["100001", "100002"],
        cutoffs_per_flight=2,
    )
    assert request.batch == "scat20161015_20161021"
    assert request.external_ids == ("100001", "100002")
    with pytest.raises(ValidationError, match="不能重复"):
        module.PrepareComparisonBatchInput(external_ids=["100001", "100001"])


def test_scat_agent_captures_pending_action_and_routes_tool_domains():
    captured = capture_pending_action(
        {
            "messages": [
                ToolMessage(
                    content=(
                        '{"status":"awaiting_confirmation",'
                        '"operation":"prepare_random_comparison_batch",'
                        '"data":{"confirmation_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                        '"selection":{"selected_external_ids":["100001"]}}}'
                    ),
                    tool_call_id="call-1",
                )
            ]
        }
    )
    assert captured["pending_action"]["domain"] == "model"
    assert captured["pending_action"]["action_kind"] == "model_comparison"

    uav_names = {
        item.name
        for item in select_aviation_tools(
            {"messages": [HumanMessage(content="查询 UAV 0007 的传感器通道")]}
        )
    }
    assert "list_uav_channels" in uav_names
    assert "prepare_model_task" not in uav_names

    comparison_names = {
        item.name
        for item in select_aviation_tools(
            {"messages": [HumanMessage(content="用 GBAR 做四方法统一对比")]}
        )
    }
    assert "prepare_comparison_batch" in comparison_names
    assert "get_uav_samples" not in comparison_names


@pytest.mark.asyncio
async def test_model_catalog_uses_explicit_registry(monkeypatch):
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))

    result = await module.list_task_models.ainvoke(
        {"dataset": "uav", "task_type": "anomaly_classification"}
    )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["model_key"] == "test-model"


@pytest.mark.asyncio
async def test_prepare_random_gbar_batch_freezes_selected_flights_before_confirmation(
    monkeypatch,
):
    evaluator = ComponentReference(key="regression-errors", version="1")
    model_ref = ComponentReference(key="scat-gbar-v23", version="23")
    spec = ModelSpec(
        model_key=model_ref.key,
        version=model_ref.version,
        display_name="GBAR",
        description="Test GBAR registration.",
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        supported_datasets={DatasetKind.SCAT},
        input_schema="regression-task-input/1",
        output_schema="regression-task-output/1",
        input_resolver=ComponentReference(key="scat-resolver", version="1"),
        model_package=model_ref,
        evaluator=evaluator,
        allowed_evaluators=(evaluator,),
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            min_input_points=71,
            max_input_points=2_000,
            max_output_points=36,
            timeout_seconds=120,
            max_batch_size=16,
        ),
        availability=ModelAvailability.AVAILABLE,
    )
    sample = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "requested_count": 3,
                "selected_count": 3,
                "eligible_count": 25,
                "external_ids": ["100002", "100014", "100020"],
            },
            "query": {
                "batch": "scat20161015_20161021",
                "aircraft_type": "A320",
                "adep": "ESSA",
            },
        }
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([spec]))
    monkeypatch.setattr(module.random_batch_service, "sample_gbar_model_flights", sample)
    monkeypatch.setattr(module.flight_database, "connect", AsyncMock())

    result = await module.prepare_random_gbar_batch.ainvoke(
        {
            "batch": "scat20161015_20161021",
            "sample_size": 3,
            "source_limit": 300,
            "aircraft_type": "a320",
            "adep": "essa",
        },
        config=_config("random-thread"),
    )

    assert result["status"] == "awaiting_confirmation"
    selection = result["data"]["selection"]
    assert selection["mode"] == "random"
    assert selection["requested_count"] == 3
    assert selection["eligible_count"] == 25
    assert selection["criteria"]["aircraft_type"] == "A320"
    assert selection["selected_external_ids"] == ["100002", "100014", "100020"]
    assert [item["external_id"] for item in result["data"]["input_refs"]] == [
        "100002",
        "100014",
        "100020",
    ]
    assert result["data"]["evaluator"] == {"key": "regression-errors", "version": "1"}


@pytest.mark.asyncio
async def test_prepare_random_comparison_batch_freezes_ids_and_builds_comparison_request(
    monkeypatch,
):
    sample = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "requested_count": 3,
                "selected_count": 3,
                "eligible_count": 64,
                "external_ids": ["100002", "100014", "100020"],
            },
            "query": {
                "batch": "scat20161015_20161021",
                "min_track_points": 171,
                "has_fpl": True,
                "has_scat_prediction": True,
                "eligibility_profile": "gbar-align-v1-random-selection",
            },
        }
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_comparison_spec()]))
    monkeypatch.setattr(
        module.random_batch_service,
        "sample_comparison_model_flights",
        sample,
    )
    monkeypatch.setattr(module.flight_database, "connect", AsyncMock())

    result = await module.prepare_random_comparison_batch.ainvoke(
        {
            "batch": r"scat20161015\_20161021",
            "sample_size": 3,
            "cutoffs_per_flight": 1,
            "aircraft_type": "b738",
        },
        config=_config("comparison-random-thread"),
    )

    assert result["status"] == "awaiting_confirmation"
    assert result["operation"] == "prepare_random_comparison_batch"
    sample.assert_awaited_once()
    assert sample.await_args.kwargs["batch"] == "scat20161015_20161021"
    assert sample.await_args.kwargs["aircraft_type"] == "B738"
    assert result["data"]["model"]["model_key"] == "scat-comparison-v1"
    assert result["data"]["parameters"] == {"cutoffs_per_flight": 1}
    assert result["data"]["evaluator"] is None
    assert [item["external_id"] for item in result["data"]["input_refs"]] == [
        "100002",
        "100014",
        "100020",
    ]
    assert {item["limit"] for item in result["data"]["input_refs"]} == {2_000}
    selection = result["data"]["selection"]
    assert selection["mode"] == "random"
    assert selection["eligible_count"] == 64
    assert selection["selected_external_ids"] == ["100002", "100014", "100020"]
    assert selection["cutoffs_per_flight"] == 1


@pytest.mark.asyncio
async def test_prepare_explicit_comparison_batch_validates_and_freezes_ids(monkeypatch):
    search = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "count": 2,
                "total": 2,
                "items": [
                    {"source": {"external_id": "100002"}},
                    {"source": {"external_id": "100014"}},
                ],
            },
        }
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_comparison_spec()]))
    monkeypatch.setattr(module.random_batch_service, "search_flights", search)
    monkeypatch.setattr(module.flight_database, "connect", AsyncMock())

    result = await module.prepare_comparison_batch.ainvoke(
        {
            "batch": "scat20161015_20161021",
            "external_ids": ["100014", "100002"],
            "cutoffs_per_flight": 2,
        },
        config=_config("comparison-explicit-thread"),
    )

    assert result["status"] == "awaiting_confirmation"
    assert result["operation"] == "prepare_comparison_batch"
    assert result["data"]["selection"]["mode"] == "explicit"
    assert result["data"]["selection"]["selected_external_ids"] == ["100014", "100002"]
    assert [item["external_id"] for item in result["data"]["input_refs"]] == [
        "100014",
        "100002",
    ]
    assert search.await_args.kwargs["min_track_points"] == 171
    assert search.await_args.kwargs["has_fpl"] is True
    assert search.await_args.kwargs["has_scat_prediction"] is True


@pytest.mark.asyncio
async def test_prepare_then_submit_consumes_single_use_confirmation(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    """同一确认码第一次提交成功，第二次重放必须失败。"""

    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    invoke = AsyncMock(
        return_value={
            "run_id": "run-1",
            "status": "succeeded",
            "progress": 1.0,
            "message": "模型任务执行完成。",
        }
    )
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)
    prepared = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")}
    )
    confirmation_id = prepared["data"]["confirmation_id"]

    submitted = await module.submit_model_task.ainvoke({"confirmation_id": confirmation_id})
    replayed = await module.submit_model_task.ainvoke({"confirmation_id": confirmation_id})

    assert prepared["status"] == "awaiting_confirmation"
    assert submitted["status"] == "succeeded"
    assert replayed["status"] == "invalid_confirmation"
    invoke.assert_awaited_once()
    executed_request = invoke.await_args.args[0]
    assert executed_request.confirmed is True
    assert executed_request.input_ref.external_id == "flight-1"
    repository = _isolated_analysis_run_repository
    repository.create.assert_awaited_once()
    created = repository.create.await_args.args[0]
    assert created.owner_user_id == "thread:direct-model-tool"
    assert created.status == ModelRunStatus.QUEUED
    assert created.input_refs[0].external_id == "flight-1"
    assert repository.transition.await_count == 2
    assert repository.transition.await_args_list[0].kwargs["status"] == ModelRunStatus.RUNNING
    assert repository.transition.await_args_list[1].kwargs["status"] == ModelRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_confirmation_is_bound_to_thread_and_user(monkeypatch):
    """确认句柄同时绑定 thread 与 user，任一不匹配都不能消费。"""

    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    invoke = AsyncMock(
        return_value={
            "run_id": "run-1",
            "status": "succeeded",
            "progress": 1.0,
            "message": "模型任务执行完成。",
        }
    )
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)
    prepared = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")},
        config=_config("thread-1"),
    )
    confirmation_id = prepared["data"]["confirmation_id"]

    wrong_thread = await module.submit_model_task.ainvoke(
        {"confirmation_id": confirmation_id},
        config=_config("thread-2"),
    )
    wrong_user = await module.submit_model_task.ainvoke(
        {"confirmation_id": confirmation_id},
        config=_config("thread-1", "user-2"),
    )
    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": confirmation_id},
        config=_config("thread-1"),
    )

    assert wrong_thread["status"] == "invalid_confirmation"
    assert wrong_user["status"] == "invalid_confirmation"
    assert submitted["status"] == "succeeded"
    invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_revokes_confirmation_in_same_scope(monkeypatch):
    """同作用域取消后，即使持有旧 ID 也不能再次提交。"""

    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    invoke = AsyncMock()
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)
    config = _config("thread-cancel")
    prepared = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")},
        config=config,
    )
    confirmation_id = prepared["data"]["confirmation_id"]

    cancelled = await module.cancel_model_task.ainvoke(
        {"confirmation_id": confirmation_id},
        config=config,
    )
    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": confirmation_id},
        config=config,
    )

    assert cancelled["status"] == "cancelled"
    assert submitted["status"] == "invalid_confirmation"
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_rejects_unknown_or_expired_confirmation(monkeypatch):
    """未知、过期或已消费的句柄统一按无效确认处理，不调用模型。"""

    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    invoke = AsyncMock()
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)

    result = await module.submit_model_task.ainvoke({"confirmation_id": "f" * 32})

    assert result["status"] == "invalid_confirmation"
    assert "重新准备" in result["message"]
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_model_run_is_scoped_to_current_user(
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    record = module._new_analysis_run(
        run_id="a" * 32,
        request=_request(confirmed=True),
        spec=_spec(),
        thread_id="thread-1",
        user_id="user-1",
    )
    repository.get_for_owner.return_value = record

    result = await module.get_model_run.ainvoke(
        {"run_id": "a" * 32},
        config=_config("thread-2", "user-1"),
    )

    assert result["status"] == "ok"
    assert result["data"]["run_id"] == "a" * 32
    assert result["data"]["model"] == {"key": "test-model", "version": "1.0.0"}
    repository.get_for_owner.assert_awaited_once_with(
        "a" * 32,
        owner_user_id="user-1",
    )


@pytest.mark.asyncio
async def test_submit_persists_safe_failed_state(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    monkeypatch.setattr(
        module,
        "_invoke_model_agent",
        AsyncMock(
            return_value={
                "status": "failed",
                "progress": 1.0,
                "message": "模型执行失败。",
                "error_code": "model_execution_failed",
            }
        ),
    )
    prepared = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")}
    )

    result = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]}
    )

    assert result["status"] == "failed"
    final = repository.transition.await_args_list[1].kwargs
    assert final["status"] == ModelRunStatus.FAILED
    assert final["error"].code == "model_execution_failed"
    assert "private" not in final["error"].message


@pytest.mark.asyncio
async def test_submit_does_not_execute_when_run_record_creation_fails(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    repository.create.side_effect = ConnectionFailure("database unavailable")
    invoke = AsyncMock()
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)
    prepared = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")}
    )

    result = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]}
    )

    assert result["status"] == "run_persistence_failed"
    assert len(result["data"]["run_id"]) == 32
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_submit_returns_queued_before_background_execution(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    invoke = AsyncMock(
        return_value={
            "status": "succeeded",
            "progress": 1.0,
            "message": "模型任务执行完成。",
        }
    )
    monkeypatch.setattr(
        module,
        "model_registry",
        ModelRegistry([_spec(ModelExecutionMode.ASYNC)]),
    )
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)
    prepared = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")},
        config=_config("async-thread"),
    )

    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]},
        config=_config("async-thread"),
    )

    assert submitted["status"] == "queued"
    assert submitted["data"]["execution_mode"] == "async"
    assert len(submitted["data"]["run_id"]) == 32
    invoke.assert_not_awaited()
    repository.transition.assert_not_awaited()
    repository.run_executor.submit.assert_awaited_once()

    scheduled_run_id, factory = repository.run_executor.submit.await_args.args
    assert scheduled_run_id == submitted["data"]["run_id"]
    await factory()

    invoke.assert_awaited_once()
    assert repository.transition.await_args_list[0].kwargs["status"] == ModelRunStatus.RUNNING
    assert repository.transition.await_args_list[1].kwargs["status"] == ModelRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_prepare_reports_unregistered_model_without_confirmation(monkeypatch):
    monkeypatch.setattr(module, "model_registry", ModelRegistry())

    result = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")}
    )

    assert result["status"] == "model_unavailable"
    assert result["data"] is None


@pytest.mark.asyncio
async def test_prepare_rejects_evaluator_outside_model_allowlist(monkeypatch):
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    request = _request().model_copy(
        update={"evaluator": ComponentReference(key="regression-errors", version="1")}
    )

    result = await module.prepare_model_task.ainvoke({"request": request.model_dump(mode="json")})

    assert result["status"] == "evaluator_not_allowed"
    assert result["data"] is None


@pytest.mark.asyncio
async def test_prepare_defaults_historical_regression_to_model_evaluator(monkeypatch):
    evaluator = ComponentReference(key="regression-errors", version="1")
    package = ComponentReference(key="test-package", version="1")
    spec = _spec().model_copy(
        update={
            "model_package": package,
            "evaluator": evaluator,
            "allowed_evaluators": (evaluator,),
        }
    )
    request = _request().model_copy(
        update={
            "input_ref": _request().input_ref.model_copy(
                update={"end_time": datetime(2026, 9, 15, tzinfo=UTC)}
            )
        }
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([spec]))

    result = await module.prepare_model_task.ainvoke({"request": request.model_dump(mode="json")})

    assert result["status"] == "awaiting_confirmation"
    assert result["data"]["evaluator"] == {"key": "regression-errors", "version": "1"}


@pytest.mark.asyncio
async def test_prepare_defaults_tail_prediction_to_automatic_holdout_evaluation(monkeypatch):
    evaluator = ComponentReference(key="regression-errors", version="1")
    spec = _spec().model_copy(
        update={
            "model_package": ComponentReference(key="test-package", version="1"),
            "evaluator": evaluator,
            "allowed_evaluators": (evaluator,),
        }
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([spec]))

    result = await module.prepare_model_task.ainvoke(
        {"request": _request().model_dump(mode="json")}
    )

    assert result["status"] == "awaiting_confirmation"
    assert result["data"]["evaluator"] == {"key": "regression-errors", "version": "1"}


@pytest.mark.asyncio
async def test_submit_attaches_regression_analysis_result(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    request = _request()
    output = RegressionTaskOutput(
        model_key="test-model",
        model_version="1.0.0",
        target_names=("value",),
        target_units={"value": "unit"},
        series=(
            RegressionPredictionSeries(
                input_ref=request.input_ref,
                forecast_origin=datetime(2026, 9, 15, tzinfo=UTC),
                time_offsets_seconds=(1.0, 2.0),
                values=((3.0,), (4.0,)),
            ),
        ),
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    monkeypatch.setattr(
        module,
        "_invoke_model_agent",
        AsyncMock(
            return_value={
                "run_id": "run-1",
                "status": "succeeded",
                "progress": 1.0,
                "message": "模型任务执行完成。",
                "_regression_output": output,
            }
        ),
    )
    prepared = await module.prepare_model_task.ainvoke({"request": request.model_dump(mode="json")})

    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]}
    )

    assert submitted["analysis_result"]["schema_version"] == "1.4"
    assert submitted["analysis_result"]["blocks"][1]["type"] == "prediction"
    assert submitted["data"]["result_summary"]["point_count"] == 2
    # 语义化 ID：任务-模型-数据集-批次-对象-时域-点数-时间-后缀
    artifact_id = submitted["data"]["artifact_id"]
    assert artifact_id.startswith("anomaly_classification-test-model-uav")
    assert "-x-flight-1-h" in artifact_id and "-n2-" in artifact_id
    repository = _isolated_analysis_run_repository
    repository.artifact_repository.create.assert_awaited_once()
    artifact = repository.artifact_repository.create.await_args.args[0]
    assert artifact.run_id == submitted["data"]["run_id"]
    assert artifact.analysis_result.schema_version == "1.4"
    action_link = submitted["analysis_result"]["blocks"][-1]
    assert action_link["type"] == "action_link"
    assert action_link["artifact_id"] == artifact.artifact_id
    assert f"/visualizations/{artifact.artifact_id}/3d" in action_link["url"]
    assert repository.transition.await_args_list[1].kwargs["result_artifact_ids"] == (
        artifact.artifact_id,
    )


@pytest.mark.asyncio
async def test_submit_attaches_comparison_table_artifact(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    origin = datetime(2026, 9, 15, tzinfo=UTC)
    input_ref = ModelInputReference(
        dataset=DatasetKind.SCAT,
        batch="batch",
        external_id="flight-1",
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.MODEL_COMPARISON,
        model_key="comparison-model",
        input_ref=input_ref,
    )
    point = ComparisonTrajectoryPoint(
        timestamp=origin + timedelta(seconds=10),
        latitude_deg=60,
        longitude_deg=20,
        altitude_m=3_000,
    )
    methods = ("gbar", "kalman-cv", "constant-velocity-heading", "cfpl")
    distribution = ComparisonMetricDistribution(mean=10, median=10, p90=10)
    output = ComparisonTaskOutput(
        model_key="comparison-model",
        model_version="1",
        sample_set_sha256="b" * 64,
        samples=(
            ComparisonSampleResult(
                sample_id="flight-1:cutoff-1",
                input_ref=input_ref,
                forecast_origin=origin,
                time_offsets_seconds=(10.0,),
                actual_points=(point,),
                method_results=tuple(
                    ComparisonMethodResult(
                        method_id=method,
                        method_version="1",
                        predicted_points=(point,),
                        ade_metres=10,
                        fde_metres=10,
                    )
                    for method in methods
                ),
            ),
        ),
        method_summaries=tuple(
            ComparisonMethodSummary(
                method_id=method,
                display_name=method,
                method_version="1",
                sample_count=1,
                covered_flights=1,
                ade_metres=distribution,
                fde_metres=distribution,
            )
            for method in methods
        ),
    )
    spec = ModelSpec(
        model_key="comparison-model",
        version="1",
        display_name="Comparison model",
        description="Comparison model test.",
        task_type=ModelTaskType.MODEL_COMPARISON,
        supported_datasets={DatasetKind.SCAT},
        input_schema=COMPARISON_TASK_INPUT_SCHEMA,
        output_schema=COMPARISON_TASK_OUTPUT_SCHEMA,
        execution_mode=ModelExecutionMode.SYNC,
        resource_limits=ModelResourceLimits(
            max_input_points=2_000,
            max_output_points=144,
            timeout_seconds=5,
        ),
        availability=ModelAvailability.AVAILABLE,
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([spec]))
    monkeypatch.setattr(
        module,
        "_invoke_model_agent",
        AsyncMock(
            return_value={
                "status": "succeeded",
                "progress": 1.0,
                "message": "模型任务执行完成。",
                "_comparison_output": output,
            }
        ),
    )
    prepared = await module.prepare_model_task.ainvoke({"request": request.model_dump(mode="json")})

    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]}
    )

    assert submitted["data"]["result_summary"]["sample_set_sha256"] == "b" * 64
    assert submitted["analysis_result"]["schema_version"] == "1.6"
    assert submitted["analysis_result"]["blocks"][0]["type"] == "table"
    assert submitted["analysis_result"]["blocks"][0]["rows"][0]["method_id"] == "gbar"
    assert submitted["analysis_result"]["blocks"][-2]["type"] == "trajectory_2d"
    assert submitted["analysis_result"]["blocks"][-1]["type"] == "action_link"
    artifact = _isolated_analysis_run_repository.artifact_repository.create.await_args.args[0]
    assert artifact.analysis_result.operation == "run_model_comparison"
    assert submitted["analysis_result"]["blocks"][-1]["artifact_id"] == artifact.artifact_id


@pytest.mark.asyncio
async def test_submit_does_not_rerun_when_artifact_persistence_fails(
    monkeypatch,
    _isolated_analysis_run_repository,
):
    request = _request()
    output = RegressionTaskOutput(
        model_key="test-model",
        model_version="1.0.0",
        target_names=("value",),
        target_units={"value": "unit"},
        series=(
            RegressionPredictionSeries(
                input_ref=request.input_ref,
                forecast_origin=datetime(2026, 9, 15, tzinfo=UTC),
                time_offsets_seconds=(1.0,),
                values=((3.0,),),
            ),
        ),
    )
    repository = _isolated_analysis_run_repository
    repository.artifact_repository.create.side_effect = ConnectionFailure("database unavailable")
    invoke = AsyncMock(
        return_value={
            "status": "succeeded",
            "progress": 1.0,
            "message": "模型任务执行完成。",
            "_regression_output": output,
        }
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    monkeypatch.setattr(module, "_invoke_model_agent", invoke)
    prepared = await module.prepare_model_task.ainvoke({"request": request.model_dump(mode="json")})

    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]}
    )

    assert submitted["status"] == "run_persistence_failed"
    assert submitted["data"]["execution_status"] == "succeeded"
    assert submitted["data"]["persistence_status"] == "failed"
    assert "请勿自动重复执行" in submitted["message"]
    invoke.assert_awaited_once()
    final = repository.transition.await_args_list[1].kwargs
    assert final["status"] == ModelRunStatus.FAILED
    assert final["error"].code == "artifact_persistence_failed"
    assert final["result_artifact_ids"] == ()


@pytest.mark.asyncio
async def test_submit_attaches_regression_error_metrics_to_summary_and_analysis(monkeypatch):
    request = _request()
    output = RegressionTaskOutput(
        model_key="test-model",
        model_version="1.0.0",
        target_names=("value",),
        target_units={"value": "unit"},
        series=(
            RegressionPredictionSeries(
                input_ref=request.input_ref,
                forecast_origin=datetime(2026, 9, 15, tzinfo=UTC),
                time_offsets_seconds=(1.0,),
                values=((3.0,),),
                expected_values=((2.0,),),
            ),
        ),
    )
    evaluation = RegressionEvaluation(
        evaluator_key="regression-errors",
        evaluator_version="1",
        target_errors=(
            RegressionTargetError(
                target_name="value",
                unit="unit",
                sample_count=1,
                rmse=1.0,
                mae=1.0,
            ),
        ),
        evaluated_series_count=1,
        skipped_series_count=0,
    )
    monkeypatch.setattr(module, "model_registry", ModelRegistry([_spec()]))
    monkeypatch.setattr(
        module,
        "_invoke_model_agent",
        AsyncMock(
            return_value={
                "run_id": "run-1",
                "status": "succeeded",
                "progress": 1.0,
                "message": "模型任务执行完成。",
                "_regression_output": output,
                "_regression_evaluation": evaluation,
            }
        ),
    )
    prepared = await module.prepare_model_task.ainvoke({"request": request.model_dump(mode="json")})

    submitted = await module.submit_model_task.ainvoke(
        {"confirmation_id": prepared["data"]["confirmation_id"]}
    )

    assert submitted["data"]["result_summary"]["error_analysis"]["target_errors"][0]["rmse"] == 1.0
    titles = [
        block["title"] for block in submitted["analysis_result"]["blocks"] if "title" in block
    ]
    assert "回归误差摘要" in titles
    assert "各特征误差" in titles


@pytest.mark.asyncio
async def test_get_analysis_artifact_restores_structured_result(
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    run_record = module._new_analysis_run(
        run_id="a" * 32,
        request=_request(confirmed=True),
        spec=_spec(),
        thread_id="thread-1",
        user_id="user-1",
    )
    from schema.analysis import AnalysisResult, MetricItem, MetricsBlock

    analysis = AnalysisResult(
        dataset="uav",
        operation="test_model",
        title="Test result",
        blocks=[
            MetricsBlock(
                title="Summary",
                items=[MetricItem(key="count", label="Count", value=1)],
            )
        ],
    )
    artifact = module._new_analysis_artifact(
        artifact_id="b" * 32,
        run_record=run_record,
        analysis_result=analysis,
    )
    repository.artifact_repository.get_for_owner.return_value = artifact

    result = await module.get_analysis_artifact.ainvoke(
        {"artifact_id": "b" * 32},
        config=_config("another-thread", "user-1"),
    )

    assert result["status"] == "ok"
    assert result["data"]["run_id"] == "a" * 32
    assert result["analysis_result"]["title"] == "Test result"
    repository.artifact_repository.get_for_owner.assert_awaited_once_with(
        "b" * 32,
        owner_user_id="user-1",
    )


@pytest.mark.asyncio
async def test_list_model_runs_and_artifacts_are_owner_scoped(
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    run_record = module._new_analysis_run(
        run_id="a" * 32,
        request=_request(confirmed=True),
        spec=_spec(),
        thread_id="thread-1",
        user_id="user-1",
    )
    from schema.analysis import AnalysisResult, MetricItem, MetricsBlock

    analysis = AnalysisResult(
        dataset="uav",
        operation="test_model",
        title="Test result",
        blocks=[
            MetricsBlock(
                title="Summary",
                items=[MetricItem(key="count", label="Count", value=1)],
            )
        ],
    )
    artifact = module._new_analysis_artifact(
        artifact_id="b" * 32,
        run_record=run_record,
        analysis_result=analysis,
    )
    repository.list_for_owner.return_value = [run_record]
    repository.artifact_repository.list_for_owner.return_value = [artifact]

    runs = await module.list_model_runs.ainvoke(
        {"limit": 5, "model_key": "test-model"},
        config=_config("another-thread", "user-1"),
    )
    artifacts = await module.list_analysis_artifacts.ainvoke(
        {"limit": 5, "model_key": "test-model"},
        config=_config("another-thread", "user-1"),
    )

    assert runs["data"]["items"][0]["run_id"] == "a" * 32
    assert artifacts["data"]["items"][0]["artifact_id"] == "b" * 32
    assert artifacts["data"]["items"][0]["action_link"] is None
    repository.list_for_owner.assert_awaited_once_with(
        owner_user_id="user-1",
        limit=5,
        model_key="test-model",
        status=None,
    )


@pytest.mark.asyncio
async def test_cancel_model_run_stops_owned_running_task(
    _isolated_analysis_run_repository,
):
    repository = _isolated_analysis_run_repository
    repository.get_for_owner.return_value = SimpleNamespace(status=ModelRunStatus.RUNNING)

    result = await module.cancel_model_run.ainvoke(
        {"run_id": "c" * 32},
        config=_config("cancel-thread", "user-1"),
    )

    assert result["status"] == "cancelled"
    assert result["data"]["cancelled_in_process"] is True
    repository.get_for_owner.assert_awaited_once_with(
        "c" * 32,
        owner_user_id="user-1",
    )
    repository.run_executor.cancel.assert_awaited_once_with("c" * 32)
    transition = repository.transition.await_args.kwargs
    assert transition["status"] == ModelRunStatus.CANCELLED
    assert transition["expected_statuses"] == (
        ModelRunStatus.QUEUED,
        ModelRunStatus.RUNNING,
    )


def test_aviation_agent_exposes_controlled_model_tools_and_confirmation_rules():
    names = {item.name for item in aviation_tools}

    assert {
        "list_task_models",
        "prepare_model_task",
        "prepare_random_gbar_batch",
        "prepare_random_comparison_batch",
        "prepare_comparison_batch",
        "submit_model_task",
        "get_model_run",
        "list_model_runs",
        "get_analysis_artifact",
        "list_analysis_artifacts",
        "cancel_model_task",
        "cancel_model_run",
    } <= names
    assert "同一用户轮次" in AVIATION_AGENT_INSTRUCTIONS
    assert "confirmation_id" in AVIATION_AGENT_INSTRUCTIONS
    assert "不得先用 search_scat_flights 拼装候选" in AVIATION_AGENT_INSTRUCTIONS
