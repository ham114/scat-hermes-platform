"""Controlled tools through which scat-flight-agent accesses the model runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import Field, field_validator
from pymongo.errors import PyMongoError

from agents.analysis_results import (
    attach_analysis_result,
    build_comparison_model_analysis,
    build_regression_model_analysis,
)
from agents.model_agent import model_agent, public_model_agent_result
from analysis_runs import (
    AnalysisArtifactRecord,
    AnalysisArtifactTooLargeError,
    AnalysisRunComponents,
    AnalysisRunConflictError,
    AnalysisRunError,
    AnalysisRunRecord,
    AnalysisRunStatusEvent,
    analysis_artifact_repository,
    analysis_run_executor,
    analysis_run_repository,
)
from analysis_runs.artifact_contracts import (
    ARTIFACT_ID_PATTERN,
    build_semantic_artifact_id,
)
from core.redis_confirmation import redis_confirmation_store
from core.settings import settings
from scat.infrastructure.database import flight_database
from scat.schemas.tool_inputs import DEFAULT_BATCH, ScatFlightSelectionCriteria
from scat.services.flight_service import ScatFlightService
from scat_model_runtime import (
    ComparisonTaskOutput,
    ComponentReference,
    DatasetKind,
    ModelAvailability,
    ModelContract,
    ModelExecutionMode,
    ModelInputReference,
    ModelRegistryError,
    ModelRunStatus,
    ModelSpec,
    ModelTaskRequest,
    ModelTaskType,
    RegressionEvaluation,
    RegressionTaskOutput,
    model_registry,
)
from scat_model_runtime.resolvers.scat_comparison import (
    COMPARISON_MAX_CUTOFFS_PER_FLIGHT,
    COMPARISON_MIN_OBSERVATION_POINTS,
    COMPARISON_SCAN_POINTS,
    COMPARISON_TRUTH_POINTS,
)
from schema.analysis import AnalysisResult

logger = logging.getLogger(__name__)
random_batch_service = ScatFlightService()


class ModelCatalogInput(ModelContract):
    """Filters for the user-visible task-model catalog."""

    dataset: DatasetKind | None = None
    task_type: ModelTaskType | None = None
    include_unavailable: bool = False


class PrepareModelTaskInput(ModelContract):
    """A typed task to validate and present for user confirmation."""

    request: ModelTaskRequest


class PrepareRandomGbarBatchInput(ScatFlightSelectionCriteria):
    """Random SCAT candidate selection followed by one controlled batch prepare."""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    sample_size: int = Field(ge=1, le=16)
    source_limit: int = Field(default=300, ge=71, le=2_000)
    forecast_cutoff: datetime | None = Field(
        default=None,
        description=("模型历史预测截止时间；不要与 takeoff_end（计划起飞筛选上界）混用。"),
    )

    @field_validator("forecast_cutoff")
    @classmethod
    def require_forecast_cutoff_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("forecast_cutoff must include a timezone")
        return value


class PrepareRandomComparisonBatchInput(ScatFlightSelectionCriteria):
    """Random eligible SCAT selection followed by one comparison prepare."""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    sample_size: int = Field(ge=1, le=50)
    cutoffs_per_flight: int = Field(
        default=1,
        ge=1,
        le=COMPARISON_MAX_CUTOFFS_PER_FLIGHT,
        description="每架航班选择的统一预测截止点数量。",
    )
    has_fpl: Literal[True] = Field(
        default=True,
        description="统一对比强制要求存在 FPL。",
    )
    has_scat_prediction: Literal[True] = Field(
        default=True,
        description="统一对比强制要求存在 CFPL/SCAT 预测快照。",
    )

    @field_validator("batch", mode="before")
    @classmethod
    def normalize_markdown_escaped_batch(cls, value: object) -> object:
        if isinstance(value, str):
            return value.replace(r"\_", "_").strip()
        return value


class PrepareComparisonBatchInput(ModelContract):
    """Explicit SCAT flights followed by one controlled comparison prepare."""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    external_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    cutoffs_per_flight: int = Field(
        default=1,
        ge=1,
        le=COMPARISON_MAX_CUTOFFS_PER_FLIGHT,
    )

    @field_validator("batch", mode="before")
    @classmethod
    def normalize_batch(cls, value: object) -> object:
        if isinstance(value, str):
            return value.replace(r"\_", "_").strip()
        return value

    @field_validator("external_ids")
    @classmethod
    def normalize_external_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value).strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("external_ids 不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("external_ids 不能重复")
        return normalized


class SubmitModelTaskInput(ModelContract):
    """A previously prepared task referenced only by its opaque confirmation ID.

    The full request is held server-side by the confirmation store, so the
    model never has to reconstruct a byte-exact copy of the prepared task.
    """

    confirmation_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )


class CancelModelTaskInput(ModelContract):
    """A prepared model task to revoke before it starts."""

    confirmation_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )


class GetModelRunInput(ModelContract):
    """Read one persistent model run in the current user's scope."""

    run_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )


class CancelModelRunInput(ModelContract):
    """Cancel one queued or running model task in the current user's scope."""

    run_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )


class GetAnalysisArtifactInput(ModelContract):
    """Restore one complete structured result in the current user's scope."""

    artifact_id: str = Field(
        min_length=8,
        max_length=200,
        pattern=ARTIFACT_ID_PATTERN,
    )


class ListModelRunsInput(ModelContract):
    """Filters for recent model runs in the current user's scope."""

    limit: int = Field(default=20, ge=1, le=50)
    model_key: str | None = Field(default=None, min_length=1, max_length=100)
    status: ModelRunStatus | None = None


class ListAnalysisArtifactsInput(ModelContract):
    """Filters for recent analysis artifacts in the current user's scope."""

    limit: int = Field(default=20, ge=1, le=50)
    model_key: str | None = Field(default=None, min_length=1, max_length=100)


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    request: ModelTaskRequest
    expires_at: datetime
    thread_id: str | None
    user_id: str | None


class ModelTaskConfirmationStore:
    """Process-local, expiring, single-use execution confirmations.

    Each entry binds the canonical prepared request to its confirmation ID.
    Submission only consumes the ID; the executed request is always the exact
    object that was prepared, which makes parameter drift impossible by
    construction instead of relying on the LLM to reproduce it.
    """

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(minutes=15),
        shared: bool = False,
    ) -> None:
        self._ttl = ttl
        self._shared = shared
        self._pending: dict[str, _PendingConfirmation] = {}
        self._lock = Lock()

    async def aissue(
        self,
        request: ModelTaskRequest,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, datetime]:
        if self._shared:
            shared = await redis_confirmation_store.issue(
                "model",
                request.model_dump(mode="json"),
                ttl=self._ttl,
                thread_id=thread_id,
                user_id=user_id,
            )
            if shared is not None:
                return shared
        return self.issue(request, thread_id=thread_id, user_id=user_id)

    async def aconsume(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> ModelTaskRequest | None:
        if self._shared:
            payload = await redis_confirmation_store.consume(
                "model",
                confirmation_id,
                thread_id=thread_id,
                user_id=user_id,
            )
            if payload is not None:
                try:
                    return ModelTaskRequest.model_validate(payload)
                except Exception:  # noqa: BLE001 - corrupted optional cache entry
                    logger.warning("Invalid model confirmation payload in Redis")
                    return None
        return self.consume(confirmation_id, thread_id=thread_id, user_id=user_id)

    async def arevoke(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        if self._shared and await redis_confirmation_store.revoke(
            "model",
            confirmation_id,
            thread_id=thread_id,
            user_id=user_id,
        ):
            return True
        return self.revoke(confirmation_id, thread_id=thread_id, user_id=user_id)

    def issue(
        self,
        request: ModelTaskRequest,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        confirmation_id = uuid4().hex
        expires_at = now + self._ttl
        pending = _PendingConfirmation(
            request=request,
            expires_at=expires_at,
            thread_id=thread_id,
            user_id=user_id,
        )
        with self._lock:
            self._prune(now)
            self._pending[confirmation_id] = pending
        return confirmation_id, expires_at

    def consume(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> ModelTaskRequest | None:
        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            pending = self._pending.get(confirmation_id)
            if pending is None or not self._scope_matches(
                pending,
                thread_id=thread_id,
                user_id=user_id,
            ):
                return None
            self._pending.pop(confirmation_id, None)
            return pending.request

    def revoke(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Atomically revoke one pending confirmation in the same caller scope."""

        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            pending = self._pending.get(confirmation_id)
            if pending is None or not self._scope_matches(
                pending,
                thread_id=thread_id,
                user_id=user_id,
            ):
                return False
            self._pending.pop(confirmation_id, None)
            return True

    @staticmethod
    def _scope_matches(
        pending: _PendingConfirmation,
        *,
        thread_id: str | None,
        user_id: str | None,
    ) -> bool:
        return pending.thread_id == thread_id and pending.user_id == user_id

    def _prune(self, now: datetime) -> None:
        expired = [
            confirmation_id
            for confirmation_id, pending in self._pending.items()
            if pending.expires_at <= now
        ]
        for confirmation_id in expired:
            self._pending.pop(confirmation_id, None)


confirmation_store = ModelTaskConfirmationStore(shared=True)


def _confirmation_scope(config: RunnableConfig) -> tuple[str | None, str | None]:
    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id")
    user_id = configurable.get("user_id")
    return (
        thread_id if isinstance(thread_id, str) else None,
        user_id if isinstance(user_id, str) else None,
    )


def _model_summary(spec: ModelSpec) -> dict[str, Any]:
    return {
        "model_key": spec.model_key,
        "version": spec.version,
        "display_name": spec.display_name,
        "description": spec.description,
        "task_type": spec.task_type.value,
        "supported_datasets": sorted(dataset.value for dataset in spec.supported_datasets),
        "execution_mode": spec.execution_mode.value,
        "availability": spec.availability.value,
        "unavailable_reason": spec.unavailable_reason,
        "limits": spec.resource_limits.model_dump(mode="json"),
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
        "allowed_evaluators": [item.model_dump(mode="json") for item in spec.allowed_evaluators],
        "default_evaluator": (
            spec.evaluator.model_dump(mode="json") if spec.evaluator is not None else None
        ),
    }


@tool(args_schema=ModelCatalogInput)
async def list_task_models(
    dataset: DatasetKind | None = None,
    task_type: ModelTaskType | None = None,
    include_unavailable: bool = False,
) -> dict[str, Any]:
    """列出已注册的预测、分类、评估和模型对比任务及其数据集和可用状态。"""
    availability = None if include_unavailable else ModelAvailability.AVAILABLE
    specs = model_registry.list(
        dataset=dataset,
        task_type=task_type,
        availability=availability,
    )
    return {
        "status": "ok",
        "operation": "list_task_models",
        "data": {
            "count": len(specs),
            "items": [_model_summary(spec) for spec in specs],
        },
    }


@tool(args_schema=PrepareModelTaskInput)
async def prepare_model_task(
    request: ModelTaskRequest,
    config: RunnableConfig,
) -> dict[str, Any]:
    """校验模型任务并生成参数摘要；调用后必须暂停并请求用户明确确认。"""
    request = request.model_copy(update={"confirmed": False})
    try:
        spec = model_registry.match(request)
    except ModelRegistryError as exc:
        return {
            "status": "model_unavailable",
            "operation": "prepare_model_task",
            "message": str(exc),
            "data": None,
        }
    if request.evaluator is None and spec.model_package is not None and spec.evaluator is not None:
        request = request.model_copy(update={"evaluator": spec.evaluator})
    if request.evaluator is not None and request.evaluator not in spec.allowed_evaluators:
        return {
            "status": "evaluator_not_allowed",
            "operation": "prepare_model_task",
            "message": "请求的误差评估器不在该模型允许范围内。",
            "data": None,
        }
    thread_id, user_id = _confirmation_scope(config)
    confirmation_id, expires_at = await confirmation_store.aissue(
        request,
        thread_id=thread_id,
        user_id=user_id,
    )
    return {
        "status": "awaiting_confirmation",
        "operation": "prepare_model_task",
        "message": "请向用户展示最终参数并等待明确确认，不要在当前轮次提交任务。",
        "data": {
            "confirmation_id": confirmation_id,
            "expires_at": expires_at.isoformat(),
            "model": _model_summary(spec),
            "input_refs": [
                reference.model_dump(mode="json", exclude_none=True)
                for reference in request.references
            ],
            "batch_size": len(request.references),
            "parameters": request.parameters,
            "evaluator": (
                request.evaluator.model_dump(mode="json") if request.evaluator is not None else None
            ),
        },
    }


@tool(args_schema=PrepareRandomGbarBatchInput)
async def prepare_random_gbar_batch(
    batch: str,
    sample_size: int,
    config: RunnableConfig,
    callsign: str | None = None,
    callsigns: tuple[str, ...] = (),
    exclude_callsigns: tuple[str, ...] = (),
    callsign_prefix: str | None = None,
    aircraft_type: str | None = None,
    aircraft_types: tuple[str, ...] = (),
    exclude_aircraft_types: tuple[str, ...] = (),
    adep: str | None = None,
    adeps: tuple[str, ...] = (),
    exclude_adeps: tuple[str, ...] = (),
    ades: str | None = None,
    adess: tuple[str, ...] = (),
    exclude_adess: tuple[str, ...] = (),
    external_id: str | None = None,
    external_ids: tuple[str, ...] = (),
    exclude_external_ids: tuple[str, ...] = (),
    candidate_key: str | None = None,
    takeoff_start: datetime | None = None,
    takeoff_end: datetime | None = None,
    min_track_points: int | None = None,
    max_track_points: int | None = None,
    has_fpl: bool | None = None,
    has_scat_prediction: bool | None = None,
    source_limit: int = 300,
    forecast_cutoff: datetime | None = None,
) -> dict[str, Any]:
    """随机选择满足长度要求的 SCAT 轨迹并准备一次 GBAR 批量预测。

    随机结果会在确认前完整展示并固化在服务端确认记录中；确认后不会重新
    抽样。未指定 forecast_cutoff 时，每条轨迹会自动保留末端真值用于默认
    误差分析。takeoff_start/takeoff_end 仅筛选计划起飞时间。
    """
    try:
        await flight_database.connect()
        sampled = await random_batch_service.sample_gbar_model_flights(
            batch=batch,
            sample_size=sample_size,
            observation_points=source_limit,
            callsign=callsign,
            callsigns=callsigns,
            exclude_callsigns=exclude_callsigns,
            callsign_prefix=callsign_prefix,
            aircraft_type=aircraft_type,
            aircraft_types=aircraft_types,
            exclude_aircraft_types=exclude_aircraft_types,
            adep=adep,
            adeps=adeps,
            exclude_adeps=exclude_adeps,
            ades=ades,
            adess=adess,
            exclude_adess=exclude_adess,
            external_id=external_id,
            external_ids=external_ids,
            exclude_external_ids=exclude_external_ids,
            candidate_key=candidate_key,
            takeoff_start=takeoff_start,
            takeoff_end=takeoff_end,
            min_track_points=min_track_points,
            max_track_points=max_track_points,
            has_fpl=has_fpl,
            has_scat_prediction=has_scat_prediction,
            end_time=forecast_cutoff,
        )
    except ValueError as exc:
        return {
            "status": "invalid_request",
            "operation": "prepare_random_gbar_batch",
            "message": str(exc),
            "data": None,
        }
    except PyMongoError:
        logger.exception("MongoDB error while sampling random GBAR batch")
        return {
            "status": "database_error",
            "operation": "prepare_random_gbar_batch",
            "message": "SCAT 随机轨迹抽样失败，请稍后重试或检查数据库服务状态。",
            "data": None,
        }
    sampled_data = sampled["data"]
    external_ids = sampled_data["external_ids"]
    if len(external_ids) != sample_size:
        return {
            "status": "not_enough_candidates",
            "operation": "prepare_random_gbar_batch",
            "message": (
                f"批次中只有 {len(external_ids)} 条轨迹同时满足观测段和误差分析真值段要求，"
                f"不足以随机选择 {sample_size} 条。"
            ),
            "data": sampled_data,
        }
    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch=batch,
            external_id=external_id,
            end_time=forecast_cutoff,
            limit=source_limit,
        )
        for external_id in external_ids
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.TRAJECTORY_PREDICTION,
        model_key="scat-gbar-v23",
        model_version="23",
        input_ref=references[0] if len(references) == 1 else None,
        input_refs=references if len(references) > 1 else (),
        parameters={},
        evaluator=ComponentReference(key="regression-errors", version="1"),
    )
    prepared = await prepare_model_task.ainvoke(
        {"request": request},
        config=config,
    )
    if isinstance(prepared.get("data"), dict):
        prepared["data"] = {
            **prepared["data"],
            "selection": {
                "mode": "random",
                "requested_count": sample_size,
                "eligible_count": sampled_data["eligible_count"],
                "criteria": sampled.get("query"),
                "selected_external_ids": external_ids,
            },
        }
    prepared["operation"] = "prepare_random_gbar_batch"
    return prepared


@tool(args_schema=PrepareRandomComparisonBatchInput)
async def prepare_random_comparison_batch(
    batch: str,
    sample_size: int,
    config: RunnableConfig,
    cutoffs_per_flight: int = 1,
    callsign: str | None = None,
    callsigns: tuple[str, ...] = (),
    exclude_callsigns: tuple[str, ...] = (),
    callsign_prefix: str | None = None,
    aircraft_type: str | None = None,
    aircraft_types: tuple[str, ...] = (),
    exclude_aircraft_types: tuple[str, ...] = (),
    adep: str | None = None,
    adeps: tuple[str, ...] = (),
    exclude_adeps: tuple[str, ...] = (),
    ades: str | None = None,
    adess: tuple[str, ...] = (),
    exclude_adess: tuple[str, ...] = (),
    external_id: str | None = None,
    external_ids: tuple[str, ...] = (),
    exclude_external_ids: tuple[str, ...] = (),
    candidate_key: str | None = None,
    takeoff_start: datetime | None = None,
    takeoff_end: datetime | None = None,
    min_track_points: int | None = None,
    max_track_points: int | None = None,
    has_fpl: bool = True,
    has_scat_prediction: bool = True,
) -> dict[str, Any]:
    """随机选择并冻结统一预测对比航班，然后准备一个受控 comparison 任务。

    仅用于 `scat-comparison-v1:1`：MongoDB `$sample` 从同时满足 GBAR 观测/
    真值点数、FPL 与 SCAT/CFPL 快照存在性条件的航班中选择 1～50 架。选择结果
    会在确认前公开并绑定到一次性 confirmation ID；提交时不会重新抽样。

    `cutoffs_per_flight` 为 1～3；每条模型引用固定 `limit=2000`，这个读取上限
    不是 `min_track_points`。本工具只准备任务，调用后必须等待用户新一轮明确确认，
    再使用 `submit_model_task`。
    """
    if not has_fpl or not has_scat_prediction:
        return {
            "status": "invalid_request",
            "operation": "prepare_random_comparison_batch",
            "message": "统一预测对比随机抽样必须要求 FPL 和 SCAT/CFPL 预测快照。",
            "data": None,
        }
    try:
        await flight_database.connect()
        sampled = await random_batch_service.sample_comparison_model_flights(
            batch=batch,
            sample_size=sample_size,
            callsign=callsign,
            callsigns=callsigns,
            exclude_callsigns=exclude_callsigns,
            callsign_prefix=callsign_prefix,
            aircraft_type=aircraft_type,
            aircraft_types=aircraft_types,
            exclude_aircraft_types=exclude_aircraft_types,
            adep=adep,
            adeps=adeps,
            exclude_adeps=exclude_adeps,
            ades=ades,
            adess=adess,
            exclude_adess=exclude_adess,
            external_id=external_id,
            external_ids=external_ids,
            exclude_external_ids=exclude_external_ids,
            candidate_key=candidate_key,
            takeoff_start=takeoff_start,
            takeoff_end=takeoff_end,
            min_track_points=min_track_points,
            max_track_points=max_track_points,
        )
    except ValueError as exc:
        return {
            "status": "invalid_request",
            "operation": "prepare_random_comparison_batch",
            "message": str(exc),
            "data": None,
        }
    except PyMongoError:
        logger.exception("MongoDB error while sampling random comparison batch")
        return {
            "status": "database_error",
            "operation": "prepare_random_comparison_batch",
            "message": "统一预测对比随机航班抽样失败，请稍后重试或检查数据库服务状态。",
            "data": None,
        }

    sampled_data = sampled["data"]
    external_ids = sampled_data["external_ids"]
    if len(external_ids) != sample_size:
        return {
            "status": "not_enough_candidates",
            "operation": "prepare_random_comparison_batch",
            "message": (
                f"批次中只有 {len(external_ids)} 架航班满足统一对比的基础候选条件，"
                f"不足以随机选择 {sample_size} 架。"
            ),
            "data": sampled_data,
        }

    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch=batch,
            external_id=selected_external_id,
            limit=COMPARISON_SCAN_POINTS,
        )
        for selected_external_id in external_ids
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.MODEL_COMPARISON,
        model_key="scat-comparison-v1",
        model_version="1",
        input_ref=references[0] if len(references) == 1 else None,
        input_refs=references if len(references) > 1 else (),
        parameters={"cutoffs_per_flight": cutoffs_per_flight},
    )
    prepared = await prepare_model_task.ainvoke(
        {"request": request},
        config=config,
    )
    if isinstance(prepared.get("data"), dict):
        prepared["data"] = {
            **prepared["data"],
            "selection": {
                "mode": "random",
                "requested_count": sample_size,
                "eligible_count": sampled_data["eligible_count"],
                "criteria": sampled.get("query"),
                "selected_external_ids": external_ids,
                "cutoffs_per_flight": cutoffs_per_flight,
                "note": (
                    "已冻结随机航班；执行时仍会按每个 cutoff 的 CFPL/真值覆盖做四方法共同排除。"
                ),
            },
        }
    prepared["operation"] = "prepare_random_comparison_batch"
    return prepared


@tool(args_schema=PrepareComparisonBatchInput)
async def prepare_comparison_batch(
    batch: str,
    external_ids: tuple[str, ...],
    config: RunnableConfig,
    cutoffs_per_flight: int = 1,
) -> dict[str, Any]:
    """校验并冻结显式航班列表，准备 GBAR/Kalman/匀速/CFPL 统一对比任务。

    本工具只接受业务参数，不要求 LLM 构造底层 ``ModelTaskRequest``。航班必须
    同时满足至少 171 个轨迹点、FPL 和 SCAT/CFPL 快照存在；prepare 后仍须等待
    用户下一轮确认，提交时不会重新选择航班。
    """

    required_points = COMPARISON_MIN_OBSERVATION_POINTS + COMPARISON_TRUTH_POINTS
    try:
        await flight_database.connect()
        matched = await random_batch_service.search_flights(
            batch=batch,
            external_ids=external_ids,
            min_track_points=required_points,
            has_fpl=True,
            has_scat_prediction=True,
            sort_by="external_id",
            limit=len(external_ids),
        )
    except ValueError as exc:
        return {
            "status": "invalid_request",
            "operation": "prepare_comparison_batch",
            "message": str(exc),
            "data": None,
        }
    except PyMongoError:
        logger.exception("MongoDB error while validating explicit comparison flights")
        return {
            "status": "database_error",
            "operation": "prepare_comparison_batch",
            "message": "统一预测对比航班校验失败，请稍后重试或检查数据库服务状态。",
            "data": None,
        }

    items = matched.get("data", {}).get("items", [])
    eligible_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        external_id = source.get("external_id") if isinstance(source, dict) else None
        # Keep accepting the flat shape used by older service adapters while
        # preferring the current public flight-summary contract.
        if external_id is None:
            external_id = item.get("external_id")
        if external_id is not None:
            eligible_ids.add(str(external_id))
    missing = [external_id for external_id in external_ids if external_id not in eligible_ids]
    if missing:
        return {
            "status": "ineligible_flights",
            "operation": "prepare_comparison_batch",
            "message": "部分航班不存在或不满足统一对比的基础候选条件。",
            "data": {
                "requested_external_ids": list(external_ids),
                "eligible_external_ids": [
                    external_id for external_id in external_ids if external_id in eligible_ids
                ],
                "ineligible_external_ids": missing,
                "required_track_points": required_points,
                "requires_fpl": True,
                "requires_scat_prediction": True,
            },
        }

    references = tuple(
        ModelInputReference(
            dataset=DatasetKind.SCAT,
            batch=batch,
            external_id=external_id,
            limit=COMPARISON_SCAN_POINTS,
        )
        for external_id in external_ids
    )
    request = ModelTaskRequest(
        task_type=ModelTaskType.MODEL_COMPARISON,
        model_key="scat-comparison-v1",
        model_version="1",
        input_ref=references[0] if len(references) == 1 else None,
        input_refs=references if len(references) > 1 else (),
        parameters={"cutoffs_per_flight": cutoffs_per_flight},
    )
    prepared = await prepare_model_task.ainvoke({"request": request}, config=config)
    if isinstance(prepared.get("data"), dict):
        prepared["data"] = {
            **prepared["data"],
            "selection": {
                "mode": "explicit",
                "selected_external_ids": list(external_ids),
                "cutoffs_per_flight": cutoffs_per_flight,
                "required_track_points": required_points,
                "note": "航班 ID 已冻结；每个 cutoff 的最终可用性仍由 Resolver 统一检查。",
            },
        }
    prepared["operation"] = "prepare_comparison_batch"
    return prepared


def _effective_run_scope(
    *,
    thread_id: str | None,
    user_id: str | None,
) -> tuple[str, str]:
    """Mirror the API's stable thread fallback for direct tool invocations."""

    effective_thread_id = thread_id or "direct-model-tool"
    effective_user_id = user_id or f"thread:{effective_thread_id}"
    return effective_thread_id, effective_user_id


def _run_components(spec: ModelSpec, request: ModelTaskRequest) -> AnalysisRunComponents:
    return AnalysisRunComponents(
        input_resolver=spec.input_resolver,
        feature_pipeline=spec.feature_pipeline,
        model_package=spec.model_package,
        execution_backend=spec.execution_backend,
        execution_release=spec.execution_release,
        evaluator=request.evaluator or spec.evaluator,
    )


def _new_analysis_run(
    *,
    run_id: str,
    request: ModelTaskRequest,
    spec: ModelSpec,
    thread_id: str | None,
    user_id: str | None,
) -> AnalysisRunRecord:
    now = datetime.now(UTC)
    effective_thread_id, effective_user_id = _effective_run_scope(
        thread_id=thread_id,
        user_id=user_id,
    )
    expires_at = None
    if settings.ANALYSIS_RUN_RETENTION_DAYS > 0:
        expires_at = now + timedelta(days=settings.ANALYSIS_RUN_RETENTION_DAYS)
    return AnalysisRunRecord(
        run_id=run_id,
        owner_user_id=effective_user_id,
        thread_id=effective_thread_id,
        task_type=request.task_type,
        execution_mode=spec.execution_mode,
        model_key=spec.model_key,
        model_version=spec.version,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        input_refs=request.references,
        parameters=request.parameters,
        components=_run_components(spec, request),
        code_version=settings.ANALYSIS_CODE_VERSION,
        status=ModelRunStatus.QUEUED,
        progress=0.0,
        status_history=(
            AnalysisRunStatusEvent(
                status=ModelRunStatus.QUEUED,
                progress=0.0,
                at=now,
                message="模型任务已创建。",
            ),
        ),
        created_at=now,
        updated_at=now,
        queued_at=now,
        expires_at=expires_at,
    )


def _new_analysis_artifact(
    *,
    artifact_id: str,
    run_record: AnalysisRunRecord,
    analysis_result: AnalysisResult,
) -> AnalysisArtifactRecord:
    now = datetime.now(UTC)
    expires_at = None
    if settings.ANALYSIS_ARTIFACT_RETENTION_DAYS > 0:
        expires_at = now + timedelta(days=settings.ANALYSIS_ARTIFACT_RETENTION_DAYS)
    return AnalysisArtifactRecord.build(
        artifact_id=artifact_id,
        run_id=run_record.run_id,
        owner_user_id=run_record.owner_user_id,
        thread_id=run_record.thread_id,
        model_key=run_record.model_key,
        model_version=run_record.model_version,
        analysis_result=analysis_result,
        created_at=now,
        expires_at=expires_at,
    )


def _public_run_record(record: AnalysisRunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "status": record.status.value,
        "progress": record.progress,
        "task_type": record.task_type.value,
        "execution_mode": record.execution_mode.value,
        "model": {"key": record.model_key, "version": record.model_version},
        "input_refs": [
            item.model_dump(mode="json", exclude_none=True) for item in record.input_refs
        ],
        "components": record.components.model_dump(mode="json", exclude_none=True),
        "code_version": record.code_version,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "result_summary": record.result_summary,
        "result_artifact_ids": list(record.result_artifact_ids),
        "result_schema_version": record.result_schema_version,
        "error": record.error.model_dump(mode="json") if record.error else None,
    }


def _public_artifact_record(record: AnalysisArtifactRecord) -> dict[str, Any]:
    action_link: str | None = None
    for block in record.analysis_result.blocks:
        payload = block.model_dump(mode="json")
        if payload.get("type") == "action_link" and isinstance(payload.get("url"), str):
            action_link = payload["url"]
            break
    return {
        "artifact_id": record.artifact_id,
        "run_id": record.run_id,
        "kind": record.kind,
        "model": {"key": record.model_key, "version": record.model_version},
        "title": record.analysis_result.title,
        "dataset": record.analysis_result.dataset,
        "operation": record.analysis_result.operation,
        "payload_size_bytes": record.payload_size_bytes,
        "created_at": record.created_at.isoformat(),
        "action_link": action_link,
    }


async def _invoke_model_agent(
    request: ModelTaskRequest,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    initial_state: dict[str, Any] = {"request": request}
    if run_id is not None:
        initial_state["run_id"] = run_id
    state = await model_agent.ainvoke(initial_state)
    result = public_model_agent_result(state)
    if run_id is not None:
        # The caller-allocated ID is the durable identity even if a custom test
        # graph returns incomplete state.
        result["run_id"] = run_id
    regression_output = state.get("regression_output")
    if regression_output is not None:
        result["_regression_output"] = regression_output
    regression_evaluation = state.get("regression_evaluation")
    if regression_evaluation is not None:
        result["_regression_evaluation"] = regression_evaluation
    comparison_output = state.get("comparison_output")
    if comparison_output is not None:
        result["_comparison_output"] = comparison_output
    return result


def _regression_summary(output: RegressionTaskOutput) -> dict[str, Any]:
    predictions = output.series
    first_prediction = predictions[0]
    first = dict(zip(output.target_names, first_prediction.values[0], strict=True))
    last = dict(zip(output.target_names, first_prediction.values[-1], strict=True))
    quality = first_prediction.annotations.get("quality")
    return {
        "output_schema": output.schema_version,
        "model_key": output.model_key,
        "model_version": output.model_version,
        "batch_size": output.batch_size,
        "point_count": sum(len(item.values) for item in predictions),
        "points_per_series": [len(item.values) for item in predictions],
        "forecast_origin": first_prediction.forecast_origin.isoformat(),
        "forecast_horizon_seconds": max(item.time_offsets_seconds[-1] for item in predictions),
        "target_names": list(output.target_names),
        "target_units": output.target_units,
        "first_prediction": first,
        "last_prediction": last,
        "quality": quality,
        "items": [
            {
                "external_id": item.input_ref.external_id,
                "batch": item.input_ref.batch,
                "point_count": len(item.values),
                "forecast_origin": item.forecast_origin.isoformat(),
                "quality": item.annotations.get("quality"),
            }
            for item in predictions
        ],
    }


def _comparison_summary(output: ComparisonTaskOutput) -> dict[str, Any]:
    return {
        "output_schema": output.schema_version,
        "protocol_version": output.protocol_version,
        "model_key": output.model_key,
        "model_version": output.model_version,
        "sample_set_sha256": output.sample_set_sha256,
        "sample_count": output.batch_size,
        "covered_flights": len({item.input_ref.external_id for item in output.samples}),
        "excluded_counts": output.excluded_counts,
        "methods": [
            {
                "method_id": item.method_id,
                "method_version": item.method_version,
                "sample_count": item.sample_count,
                "covered_flights": item.covered_flights,
                "ade_metres": item.ade_metres.model_dump(mode="json"),
                "fde_metres": item.fde_metres.model_dump(mode="json"),
            }
            for item in output.method_summaries
        ],
    }


async def _execute_persisted_model_run(
    request: ModelTaskRequest,
    run_record: AnalysisRunRecord,
) -> dict[str, Any]:
    """Execute one already-created run and persist its terminal result."""

    run_id = run_record.run_id
    try:
        await analysis_run_repository.transition(
            run_id,
            expected_statuses=(ModelRunStatus.QUEUED,),
            status=ModelRunStatus.RUNNING,
            progress=0.05,
            message="模型任务开始执行。",
        )
    except (PyMongoError, AnalysisRunConflictError):
        logger.exception("Failed to start persistent analysis run")
        return {
            "status": "run_persistence_failed",
            "operation": "submit_model_task",
            "message": "运行记录无法进入执行状态，模型任务尚未启动。",
            "data": {"run_id": run_id},
        }
    try:
        result = await _invoke_model_agent(request, run_id=run_id)
        # The durable run record owns the task identity.  Keep that identity
        # even when an alternate model-agent adapter returns its own run id.
        result["run_id"] = run_id
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected model-agent execution failure")
        result = {
            "run_id": run_id,
            "status": "failed",
            "progress": 1.0,
            "message": "模型执行发生未预期错误。",
            "error_code": "model_agent_internal_error",
        }
    regression_output = result.pop("_regression_output", None)
    regression_evaluation = result.pop("_regression_evaluation", None)
    comparison_output = result.pop("_comparison_output", None)
    response = {
        "status": result["status"],
        "operation": "submit_model_task",
        "message": result.get("message"),
        "data": result,
    }
    structured_analysis: AnalysisResult | None = None
    artifact_id: str | None = None
    if result["status"] == "succeeded" and isinstance(regression_output, RegressionTaskOutput):
        result["result_summary"] = _regression_summary(regression_output)
        if isinstance(regression_evaluation, RegressionEvaluation):
            result["result_summary"]["error_analysis"] = regression_evaluation.model_dump(
                mode="json"
            )
        artifact_id = build_semantic_artifact_id(
            task=run_record.task_type,
            model_key=run_record.model_key,
            dataset=regression_output.series[0].input_ref.dataset,
            batch=regression_output.series[0].input_ref.batch,
            external_id=regression_output.series[0].input_ref.external_id,
            horizon_s=max(regression_output.series[0].time_offsets_seconds),
            n_points=len(regression_output.series[0].time_offsets_seconds),
            now=datetime.now(UTC),
            suffix=uuid4().hex[:6],
        )
        structured_analysis = build_regression_model_analysis(
            regression_output,
            regression_evaluation,
            artifact_id=artifact_id,
            thread_id=run_record.thread_id,
            visualization_base_url=settings.ANALYSIS_VISUALIZATION_BASE_URL,
        )
        response = attach_analysis_result(
            response,
            structured_analysis,
        )
    elif result["status"] == "succeeded" and isinstance(comparison_output, ComparisonTaskOutput):
        result["result_summary"] = _comparison_summary(comparison_output)
        first_sample = comparison_output.samples[0]
        artifact_id = build_semantic_artifact_id(
            task=run_record.task_type,
            model_key=run_record.model_key,
            dataset=first_sample.input_ref.dataset,
            batch=first_sample.input_ref.batch,
            external_id=(
                first_sample.input_ref.external_id
                if len({item.input_ref.external_id for item in comparison_output.samples}) == 1
                else "multi"
            ),
            horizon_s=max(sample.time_offsets_seconds[-1] for sample in comparison_output.samples),
            n_points=comparison_output.batch_size,
            now=datetime.now(UTC),
            suffix=uuid4().hex[:6],
        )
        structured_analysis = build_comparison_model_analysis(
            comparison_output,
            artifact_id=artifact_id,
            thread_id=run_record.thread_id,
            visualization_base_url=settings.ANALYSIS_VISUALIZATION_BASE_URL,
        )
        response = attach_analysis_result(response, structured_analysis)

    succeeded = result["status"] == "succeeded"
    final_status = ModelRunStatus.SUCCEEDED if succeeded else ModelRunStatus.FAILED
    error = None
    if not succeeded:
        error = AnalysisRunError(
            code=str(result.get("error_code") or "model_execution_failed"),
            message=str(result.get("message") or "模型任务执行失败。")[:500],
        )
    artifact_ids: tuple[str, ...] = ()
    if succeeded and structured_analysis is not None and artifact_id is not None:
        artifact = _new_analysis_artifact(
            artifact_id=artifact_id,
            run_record=run_record,
            analysis_result=structured_analysis,
        )
        try:
            await analysis_artifact_repository.create(artifact)
        except (PyMongoError, AnalysisArtifactTooLargeError):
            logger.exception("Failed to persist model analysis artifact")
            succeeded = False
            final_status = ModelRunStatus.FAILED
            error = AnalysisRunError(
                code="artifact_persistence_failed",
                message="模型已经执行，但完整分析结果保存失败。",
            )
            response["status"] = "run_persistence_failed"
            response["message"] = (
                "模型已经执行，但完整结果保存失败；请勿自动重复执行，请根据 run_id 排查。"
            )
            result["execution_status"] = ModelRunStatus.SUCCEEDED.value
            result["persistence_status"] = "failed"
        else:
            artifact_ids = (artifact_id,)
            result["artifact_id"] = artifact_id
            result["artifact_ids"] = [artifact_id]
            result["artifact_payload_size_bytes"] = artifact.payload_size_bytes
            result["artifact_sha256"] = artifact.payload_sha256
    try:
        await analysis_run_repository.transition(
            run_id,
            expected_statuses=(ModelRunStatus.RUNNING,),
            status=final_status,
            progress=1.0,
            message=str(result.get("message") or "模型任务执行完成。")[:500],
            error=error,
            result_summary=(
                result.get("result_summary")
                if isinstance(result.get("result_summary"), dict)
                else None
            ),
            result_schema_version=(
                structured_analysis.schema_version if structured_analysis is not None else None
            ),
            result_artifact_ids=artifact_ids,
        )
    except (PyMongoError, AnalysisRunConflictError):
        logger.exception("Failed to finalize persistent analysis run")
        response["status"] = "run_persistence_failed"
        response["message"] = (
            "模型已经执行，但运行记录更新失败；请勿自动重复执行，请根据 run_id 排查。"
        )
        result["execution_status"] = final_status.value
        result["persistence_status"] = "failed"
    return response


async def _run_background_model_task(
    request: ModelTaskRequest,
    run_record: AnalysisRunRecord,
) -> None:
    try:
        await _execute_persisted_model_run(request, run_record)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled asynchronous model-run failure run_id=%s", run_record.run_id)
        try:
            await analysis_run_repository.transition(
                run_record.run_id,
                expected_statuses=(ModelRunStatus.QUEUED, ModelRunStatus.RUNNING),
                status=ModelRunStatus.FAILED,
                progress=1.0,
                message="异步模型任务发生未预期错误。",
                error=AnalysisRunError(
                    code="model_agent_internal_error",
                    message="异步模型任务发生未预期错误。",
                ),
            )
        except (PyMongoError, AnalysisRunConflictError):
            logger.exception(
                "Failed to persist unexpected asynchronous failure run_id=%s",
                run_record.run_id,
            )


@tool(args_schema=SubmitModelTaskInput)
async def submit_model_task(
    confirmation_id: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """提交之前已准备好、且用户已明确确认的模型任务。

    同步模型在当前请求中返回结果；异步模型立即返回持久化 run_id，随后使用
    get_model_run 查询状态。确认码一次性且约 15 分钟有效。
    """

    thread_id, user_id = _confirmation_scope(config)
    request = await confirmation_store.aconsume(
        confirmation_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    if request is None:
        return {
            "status": "invalid_confirmation",
            "operation": "submit_model_task",
            "message": (
                "确认码无效、已过期或已使用。请调用 prepare_model_task "
                "重新准备任务并等待用户再次明确确认；不要重复提交同一个确认码。"
            ),
            "data": None,
        }
    confirmed = request.model_copy(update={"confirmed": True})
    try:
        spec = model_registry.match(confirmed)
    except ModelRegistryError as exc:
        return {
            "status": "model_unavailable",
            "operation": "submit_model_task",
            "message": str(exc),
            "data": None,
        }

    run_id = uuid4().hex
    run_record = _new_analysis_run(
        run_id=run_id,
        request=confirmed,
        spec=spec,
        thread_id=thread_id,
        user_id=user_id,
    )
    try:
        await analysis_run_repository.create(run_record)
    except PyMongoError:
        logger.exception("Failed to create persistent analysis run")
        return {
            "status": "run_persistence_failed",
            "operation": "submit_model_task",
            "message": "运行记录创建失败，模型任务尚未启动。请检查 MongoDB 后重新准备并确认。",
            "data": {"run_id": run_id},
        }

    if spec.execution_mode == ModelExecutionMode.ASYNC:
        try:
            await analysis_run_executor.submit(
                run_id,
                lambda: _run_background_model_task(confirmed, run_record),
            )
        except Exception:
            logger.exception("Failed to schedule asynchronous model run")
            error = AnalysisRunError(
                code="model_scheduling_failed",
                message="异步模型任务调度失败。",
            )
            try:
                await analysis_run_repository.transition(
                    run_id,
                    expected_statuses=(ModelRunStatus.QUEUED,),
                    status=ModelRunStatus.FAILED,
                    progress=1.0,
                    message=error.message,
                    error=error,
                )
            except (PyMongoError, AnalysisRunConflictError):
                logger.exception("Failed to persist asynchronous scheduling failure")
            return {
                "status": "failed",
                "operation": "submit_model_task",
                "message": error.message,
                "data": {"run_id": run_id},
            }
        return {
            "status": "queued",
            "operation": "submit_model_task",
            "message": "模型任务已进入后台队列，可使用 run_id 查询进度或取消。",
            "data": {
                "run_id": run_id,
                "status": ModelRunStatus.QUEUED.value,
                "progress": 0.0,
                "execution_mode": ModelExecutionMode.ASYNC.value,
                "model": {"key": spec.model_key, "version": spec.version},
            },
        }

    return await _execute_persisted_model_run(confirmed, run_record)


@tool(args_schema=GetAnalysisArtifactInput)
async def get_analysis_artifact(
    artifact_id: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """按 artifact_id 恢复当前用户自己的完整结构化分析结果。"""

    thread_id, user_id = _confirmation_scope(config)
    _, effective_user_id = _effective_run_scope(thread_id=thread_id, user_id=user_id)
    try:
        artifact = await analysis_artifact_repository.get_for_owner(
            artifact_id,
            owner_user_id=effective_user_id,
        )
    except PyMongoError:
        logger.exception("MongoDB error while reading analysis artifact")
        return {
            "status": "database_error",
            "operation": "get_analysis_artifact",
            "message": "完整分析结果查询失败，请稍后重试。",
            "data": None,
        }
    except ValueError:
        logger.exception("Stored analysis artifact failed contract validation")
        return {
            "status": "internal_error",
            "operation": "get_analysis_artifact",
            "message": "完整分析结果格式异常，请记录 artifact_id 并联系维护人员。",
            "data": None,
        }
    if artifact is None:
        return {
            "status": "not_found",
            "operation": "get_analysis_artifact",
            "message": "未找到该完整结果，或该结果不属于当前用户。",
            "data": None,
        }
    return {
        "status": "ok",
        "operation": "get_analysis_artifact",
        "message": "完整分析结果恢复成功。",
        "data": {
            "artifact_id": artifact.artifact_id,
            "run_id": artifact.run_id,
            "kind": artifact.kind,
            "model": {"key": artifact.model_key, "version": artifact.model_version},
            "payload_size_bytes": artifact.payload_size_bytes,
            "payload_sha256": artifact.payload_sha256,
            "created_at": artifact.created_at.isoformat(),
        },
        "analysis_result": artifact.analysis_result.model_dump(mode="json"),
    }


@tool(args_schema=GetModelRunInput)
async def get_model_run(
    run_id: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """按 run_id 查询当前用户自己的模型运行状态和有界结果摘要。"""

    thread_id, user_id = _confirmation_scope(config)
    _, effective_user_id = _effective_run_scope(thread_id=thread_id, user_id=user_id)
    try:
        record = await analysis_run_repository.get_for_owner(
            run_id,
            owner_user_id=effective_user_id,
        )
    except PyMongoError:
        logger.exception("MongoDB error while reading analysis run")
        return {
            "status": "database_error",
            "operation": "get_model_run",
            "message": "模型运行记录查询失败，请稍后重试。",
            "data": None,
        }
    except ValueError:
        logger.exception("Stored analysis run failed contract validation")
        return {
            "status": "internal_error",
            "operation": "get_model_run",
            "message": "模型运行记录格式异常，请记录 run_id 并联系维护人员。",
            "data": None,
        }
    if record is None:
        return {
            "status": "not_found",
            "operation": "get_model_run",
            "message": "未找到该运行记录，或该记录不属于当前用户。",
            "data": None,
        }
    return {
        "status": "ok",
        "operation": "get_model_run",
        "message": "模型运行记录查询成功。",
        "data": _public_run_record(record),
    }


@tool(args_schema=ListModelRunsInput)
async def list_model_runs(
    config: RunnableConfig,
    limit: int = 20,
    model_key: str | None = None,
    status: ModelRunStatus | None = None,
) -> dict[str, Any]:
    """列出当前用户最近的模型运行，用于找回遗失的 run_id。"""

    thread_id, user_id = _confirmation_scope(config)
    _, effective_user_id = _effective_run_scope(thread_id=thread_id, user_id=user_id)
    try:
        records = await analysis_run_repository.list_for_owner(
            owner_user_id=effective_user_id,
            limit=limit,
            model_key=model_key,
            status=status,
        )
    except PyMongoError:
        logger.exception("MongoDB error while listing analysis runs")
        return {
            "status": "database_error",
            "operation": "list_model_runs",
            "message": "模型运行列表查询失败，请稍后重试。",
            "data": None,
        }
    return {
        "status": "ok",
        "operation": "list_model_runs",
        "data": {
            "count": len(records),
            "items": [_public_run_record(record) for record in records],
        },
    }


@tool(args_schema=ListAnalysisArtifactsInput)
async def list_analysis_artifacts(
    config: RunnableConfig,
    limit: int = 20,
    model_key: str | None = None,
) -> dict[str, Any]:
    """列出当前用户最近的结构化结果产物，不返回大体积分析正文。"""

    thread_id, user_id = _confirmation_scope(config)
    _, effective_user_id = _effective_run_scope(thread_id=thread_id, user_id=user_id)
    try:
        records = await analysis_artifact_repository.list_for_owner(
            owner_user_id=effective_user_id,
            limit=limit,
            model_key=model_key,
        )
    except PyMongoError:
        logger.exception("MongoDB error while listing analysis artifacts")
        return {
            "status": "database_error",
            "operation": "list_analysis_artifacts",
            "message": "分析产物列表查询失败，请稍后重试。",
            "data": None,
        }
    return {
        "status": "ok",
        "operation": "list_analysis_artifacts",
        "data": {
            "count": len(records),
            "items": [_public_artifact_record(record) for record in records],
        },
    }


@tool(args_schema=CancelModelTaskInput)
async def cancel_model_task(
    confirmation_id: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """撤销尚未执行的模型任务确认码；只能撤销当前用户和对话中的任务。"""

    thread_id, user_id = _confirmation_scope(config)
    revoked = await confirmation_store.arevoke(
        confirmation_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    if not revoked:
        return {
            "status": "invalid_confirmation",
            "operation": "cancel_model_task",
            "message": "确认码无效、已过期、已使用，或不属于当前对话。",
            "data": None,
        }
    return {
        "status": "cancelled",
        "operation": "cancel_model_task",
        "message": "模型任务已取消，确认码已撤销。",
        "data": None,
    }


@tool(args_schema=CancelModelRunInput)
async def cancel_model_run(
    run_id: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """取消当前用户自己的 queued/running 异步模型任务。"""

    thread_id, user_id = _confirmation_scope(config)
    _, effective_user_id = _effective_run_scope(thread_id=thread_id, user_id=user_id)
    try:
        record = await analysis_run_repository.get_for_owner(
            run_id,
            owner_user_id=effective_user_id,
        )
    except PyMongoError:
        logger.exception("MongoDB error while reading run for cancellation")
        return {
            "status": "database_error",
            "operation": "cancel_model_run",
            "message": "模型运行记录查询失败，请稍后重试。",
            "data": None,
        }
    if record is None:
        return {
            "status": "not_found",
            "operation": "cancel_model_run",
            "message": "未找到该运行记录，或该记录不属于当前用户。",
            "data": None,
        }
    terminal_statuses = {
        ModelRunStatus.SUCCEEDED,
        ModelRunStatus.FAILED,
        ModelRunStatus.CANCELLED,
    }
    if record.status in terminal_statuses:
        return {
            "status": "already_finished",
            "operation": "cancel_model_run",
            "message": f"模型任务已经是终态 {record.status.value}，不能取消。",
            "data": _public_run_record(record),
        }

    cancelled_in_process = await analysis_run_executor.cancel(run_id)
    try:
        await analysis_run_repository.transition(
            run_id,
            expected_statuses=(ModelRunStatus.QUEUED, ModelRunStatus.RUNNING),
            status=ModelRunStatus.CANCELLED,
            progress=1.0,
            message="模型任务已由用户取消。",
        )
    except AnalysisRunConflictError:
        latest = await analysis_run_repository.get_for_owner(
            run_id,
            owner_user_id=effective_user_id,
        )
        if latest is not None and latest.status in terminal_statuses:
            return {
                "status": "already_finished",
                "operation": "cancel_model_run",
                "message": f"模型任务已经是终态 {latest.status.value}，不能取消。",
                "data": _public_run_record(latest),
            }
        return {
            "status": "conflict",
            "operation": "cancel_model_run",
            "message": "模型任务状态已经变化，请重新查询运行状态。",
            "data": {"run_id": run_id},
        }
    except PyMongoError:
        logger.exception("MongoDB error while persisting run cancellation")
        return {
            "status": "database_error",
            "operation": "cancel_model_run",
            "message": "取消状态保存失败，请根据 run_id 重新查询。",
            "data": {"run_id": run_id},
        }
    return {
        "status": "cancelled",
        "operation": "cancel_model_run",
        "message": "异步模型任务已取消。",
        "data": {
            "run_id": run_id,
            "status": ModelRunStatus.CANCELLED.value,
            "progress": 1.0,
            "cancelled_in_process": cancelled_in_process,
        },
    }


model_tools: list[BaseTool] = [
    list_task_models,
    prepare_model_task,
    prepare_random_gbar_batch,
    prepare_random_comparison_batch,
    prepare_comparison_batch,
    submit_model_task,
    get_model_run,
    list_model_runs,
    get_analysis_artifact,
    list_analysis_artifacts,
    cancel_model_task,
    cancel_model_run,
]


__all__ = [
    "CancelModelRunInput",
    "CancelModelTaskInput",
    "GetAnalysisArtifactInput",
    "GetModelRunInput",
    "ListModelRunsInput",
    "ListAnalysisArtifactsInput",
    "ModelCatalogInput",
    "ModelTaskConfirmationStore",
    "PrepareModelTaskInput",
    "PrepareRandomComparisonBatchInput",
    "PrepareComparisonBatchInput",
    "PrepareRandomGbarBatchInput",
    "SubmitModelTaskInput",
    "cancel_model_run",
    "cancel_model_task",
    "confirmation_store",
    "get_analysis_artifact",
    "get_model_run",
    "list_model_runs",
    "list_analysis_artifacts",
    "list_task_models",
    "model_tools",
    "prepare_model_task",
    "prepare_random_comparison_batch",
    "prepare_comparison_batch",
    "prepare_random_gbar_batch",
    "submit_model_task",
]
