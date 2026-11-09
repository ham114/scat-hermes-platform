"""M6-08 对话侧实验编排：prepare/submit/get/list 边界工具。

设计约束（计划 M6-08 / §5.3 / D-19）：

- 批量实验**不在对话里同步执行**：`submit` 只落一条 `pending` 实验记录，执行由确定性
  执行器领取（`python -m scat.experiments.cli claim`，与物化作业同模式）；
- 确认 ID：与完整参数绑定、15 分钟有效、单次消费、按 thread/user 作用域校验
  （与物化/模型任务确认同语义）；
- 提交链路：边界工具 → `experiment-agent`（内部确定性图：校验 → 解析预测器 →
  落 pending）；数值计算不交给 LLM；
- 口径：实验指标为 `m6-eval-v1`（自研预测器批量评估），与 CFPL 基准
  （`scat-prediction-metrics-v2`）和任务模型（GBAR）评估相互独立，数字不得混列。

工具只注册给 `scat-flight-agent`（见 `agents.scat_agent.aviation_tools`）。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import TypeAdapter

from agents.experiment_agent import experiment_agent, public_experiment_agent_result
from agents.scat_tools import _execute_service_operation
from core.redis_confirmation import redis_confirmation_store
from scat.experiments.contracts import EXPERIMENT_PROTOCOL_VERSION
from scat.experiments.repository import ExperimentRepository
from scat.infrastructure.database import flight_database
from scat.prediction.alignment import HORIZONS_S
from scat.prediction.defaults import DEFAULT_PREDICTOR_ID
from scat.prediction.registry import default_registry
from scat.repositories.flight_repository import ScatFlightRepository
from scat.schemas.tool_inputs import (
    DEFAULT_BATCH,
    GetExperimentRunInput,
    ListPredictionExperimentsInput,
    PreparePredictionExperimentInput,
    SubmitPredictionExperimentInput,
)

CONFIRMATION_TTL = timedelta(minutes=15)
DEFAULT_STRIDE_S = 60.0
DEFAULT_MAX_CUTOFFS_PER_FLIGHT = 12
logger = logging.getLogger(__name__)

_ERROR_CODE_TO_TOOL_STATUS: dict[str, str] = {
    "invalid_request": "invalid_request",
    "unsupported_experiment_kind": "invalid_request",
    "unknown_predictor": "invalid_request",
}


@dataclass(frozen=True, slots=True)
class PreparedPredictionExperiment:
    """已准备（未入队）的预测器评估实验：参数与影响面绑定在确认 ID 上。"""

    batch: str
    predictor_id: str
    predictor_version: str
    limit: int | None
    stride_s: float
    max_cutoffs_per_flight: int
    horizons_s: tuple[float, ...]
    batch_flight_count: int


_PREPARED_EXPERIMENT_ADAPTER = TypeAdapter(PreparedPredictionExperiment)


class ExperimentConfirmationStore:
    """进程内、可过期、一次性消费的实验确认（与物化/模型任务确认同语义）。

    条目把「已准备好的实验参数」与确认 ID 绑定，提交时只消费 ID、不重放参数，
    因此参数漂移在构造上不可能发生。
    """

    def __init__(
        self,
        *,
        ttl: timedelta = CONFIRMATION_TTL,
        shared: bool = False,
    ) -> None:
        self._ttl = ttl
        self._shared = shared
        self._lock = Lock()
        self._pending: dict[
            str, tuple[PreparedPredictionExperiment, datetime, str | None, str | None]
        ] = {}

    async def aissue(
        self,
        experiment: PreparedPredictionExperiment,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, datetime]:
        if self._shared:
            shared = await redis_confirmation_store.issue(
                "experiment",
                asdict(experiment),
                ttl=self._ttl,
                thread_id=thread_id,
                user_id=user_id,
            )
            if shared is not None:
                return shared
        return self.issue(experiment, thread_id=thread_id, user_id=user_id)

    async def aconsume(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> PreparedPredictionExperiment | None:
        if self._shared:
            payload = await redis_confirmation_store.consume(
                "experiment",
                confirmation_id,
                thread_id=thread_id,
                user_id=user_id,
            )
            if payload is not None:
                try:
                    return _PREPARED_EXPERIMENT_ADAPTER.validate_python(payload)
                except Exception:  # noqa: BLE001 - corrupted optional cache entry
                    logger.warning("Invalid experiment confirmation payload in Redis")
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
            "experiment",
            confirmation_id,
            thread_id=thread_id,
            user_id=user_id,
        ):
            return True
        return self.revoke(confirmation_id, thread_id=thread_id, user_id=user_id)

    def issue(
        self,
        experiment: PreparedPredictionExperiment,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        confirmation_id = uuid4().hex
        expires_at = now + self._ttl
        with self._lock:
            self._prune(now)
            self._pending[confirmation_id] = (experiment, expires_at, thread_id, user_id)
        return confirmation_id, expires_at

    def consume(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> PreparedPredictionExperiment | None:
        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            entry = self._pending.get(confirmation_id)
            if entry is None:
                return None
            experiment, _expires_at, entry_thread, entry_user = entry
            if entry_thread != thread_id or entry_user != user_id:
                return None
            self._pending.pop(confirmation_id, None)
            return experiment

    def revoke(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Revoke one pending confirmation inside the original caller scope."""

        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            entry = self._pending.get(confirmation_id)
            if entry is None:
                return False
            _experiment, _expires_at, entry_thread, entry_user = entry
            if entry_thread != thread_id or entry_user != user_id:
                return False
            self._pending.pop(confirmation_id, None)
            return True

    def pending_count(self) -> int:
        with self._lock:
            self._prune(datetime.now(UTC))
            return len(self._pending)

    def _prune(self, now: datetime) -> None:
        for confirmation_id in [key for key, entry in self._pending.items() if entry[1] <= now]:
            self._pending.pop(confirmation_id, None)


confirmation_store = ExperimentConfirmationStore(shared=True)


def _confirmation_scope(config: RunnableConfig) -> tuple[str | None, str | None]:
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id")
    user_id = configurable.get("user_id")
    return (
        thread_id if isinstance(thread_id, str) else None,
        user_id if isinstance(user_id, str) else None,
    )


def _experiment_summary(prepared: PreparedPredictionExperiment) -> dict[str, Any]:
    planned = (
        prepared.batch_flight_count
        if prepared.limit is None
        else min(prepared.batch_flight_count, prepared.limit)
    )
    return {
        "kind": "predictor_evaluation",
        "batch": prepared.batch,
        "predictor_id": prepared.predictor_id,
        "predictor_version": prepared.predictor_version,
        "protocol_version": EXPERIMENT_PROTOCOL_VERSION,
        "limit": prepared.limit,
        "stride_s": prepared.stride_s,
        "max_cutoffs_per_flight": prepared.max_cutoffs_per_flight,
        "horizons_s": list(prepared.horizons_s),
        "batch_flight_count": prepared.batch_flight_count,
        "planned_flight_count": planned,
    }


def _executor_hint(experiment_id: str) -> str:
    return (
        "平台执行器（cron 每分钟轮询）将自动领取执行，无需人工操作；如需手动执行："
        f"PYTHONPATH=src python -m scat.experiments.cli claim --experiment-id {experiment_id}"
    )


@tool(args_schema=PreparePredictionExperimentInput)
async def prepare_prediction_experiment(
    batch: str = DEFAULT_BATCH,
    predictor_id: str = DEFAULT_PREDICTOR_ID,
    limit: int | None = None,
    stride_s: float = DEFAULT_STRIDE_S,
    max_cutoffs_per_flight: int = DEFAULT_MAX_CUTOFFS_PER_FLIGHT,
    horizons_s: list[float] | None = None,
    *,
    config: RunnableConfig,
) -> dict[str, Any]:
    """准备一次自研预测器批量评估实验，返回参数摘要、影响面与一次性确认 ID。

    **不写数据库、不执行计算**：只统计该批次共有多少航班。实验口径为 `m6-eval-v1`：
    在数百至上万条航班上按观测截止点采样（步长 `stride_s`、单航班最多
    `max_cutoffs_per_flight` 个截止点），对六档时域（缺省 60/180/300/600/900/1200 秒）
    计算水平误差并聚合 MAE/中位数/P90/P95 与最差样本。

    拿到 confirmation_id 后，必须先向用户明确说明将要执行的模型、批次与规模并取得确认，
    再调用 `submit_prediction_experiment(confirmation_id)` 入队；确认 ID 15 分钟有效、
    只能用一次。入队后由平台执行器自动领取执行（cron 每分钟轮询，对话不会同步跑批）；
    全批执行可达数十分钟到 1 小时，须向用户说明可稍后用 get_experiment_run 查询。

    适用问题：
    - 在这批数据上跑一次 Kalman/匀速基线的完整评估；
    - 两个预测器/不同参数在相同批次上的批量对比（各自 prepare + 确认 + submit）。
    """
    normalized_batch = str(batch).strip()
    if not normalized_batch:
        return {
            "status": "invalid_request",
            "operation": "prepare_prediction_experiment",
            "message": "必须提供 batch。",
            "data": None,
        }
    try:
        predictor = default_registry.get(predictor_id)
    except KeyError:
        available = ", ".join(pid for pid, _version in default_registry.list_predictors())
        return {
            "status": "invalid_request",
            "operation": "prepare_prediction_experiment",
            "message": f"未知预测器 {predictor_id!r}；可用: {available}",
            "data": None,
        }
    resolved_horizons = tuple(
        float(value) for value in (horizons_s if horizons_s is not None else HORIZONS_S)
    )

    async def operation() -> dict[str, Any]:
        total_count = await ScatFlightRepository().count_all(batch=normalized_batch)
        if total_count <= 0:
            return {
                "status": "not_found",
                "operation": "prepare_prediction_experiment",
                "message": f"批次 {normalized_batch} 中没有航班数据。",
                "data": None,
            }
        prepared = PreparedPredictionExperiment(
            batch=normalized_batch,
            predictor_id=predictor.predictor_id,
            predictor_version=predictor.predictor_version,
            limit=limit,
            stride_s=float(stride_s),
            max_cutoffs_per_flight=int(max_cutoffs_per_flight),
            horizons_s=resolved_horizons,
            batch_flight_count=total_count,
        )
        thread_id, user_id = _confirmation_scope(config)
        confirmation_id, expires_at = await confirmation_store.aissue(
            prepared,
            thread_id=thread_id,
            user_id=user_id,
        )
        return {
            "status": "awaiting_confirmation",
            "operation": "prepare_prediction_experiment",
            "confirmation_id": confirmation_id,
            "expires_at": expires_at.isoformat(),
            "data": {
                **_experiment_summary(prepared),
                "note": "尚未入队；请向用户确认后再调用 submit_prediction_experiment。",
            },
        }

    return await _execute_service_operation(
        operation_name="prepare_prediction_experiment",
        operation=operation,
    )


@tool(args_schema=SubmitPredictionExperimentInput)
async def submit_prediction_experiment(
    confirmation_id: str,
    *,
    config: RunnableConfig,
) -> dict[str, Any]:
    """提交（入队）已准备的实验：只落一条 pending 实验记录，不执行计算。

    - 只接受 `prepare_prediction_experiment` 返回的确认 ID（15 分钟有效、单次消费）；
    - 入队后由平台执行器自动领取执行（分钟级启动）；提交后不得在同一轮内轮询，
      稍后用 `get_experiment_run(experiment_id)` 查询进度与指标；
    - 绝不允许由模型直接指定参数绕过 prepare/确认流程。
    """
    thread_id, user_id = _confirmation_scope(config or {})
    prepared = await confirmation_store.aconsume(
        confirmation_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    if prepared is None:
        return {
            "status": "invalid_confirmation",
            "operation": "submit_prediction_experiment",
            "message": "确认 ID 无效、已过期或已被使用；请重新调用 prepare_prediction_experiment。",
            "data": None,
        }

    async def operation() -> dict[str, Any]:
        state = await experiment_agent.ainvoke(
            {
                "request": {
                    "experiment_kind": "predictor_evaluation",
                    "batch": prepared.batch,
                    "predictor_id": prepared.predictor_id,
                    "limit": prepared.limit,
                    "stride_s": prepared.stride_s,
                    "max_cutoffs_per_flight": prepared.max_cutoffs_per_flight,
                    "horizons_s": list(prepared.horizons_s),
                },
                "requested_by": user_id,
            }
        )
        result = public_experiment_agent_result(state)
        if result.get("status") != "queued":
            error_code = str(result.get("error_code") or "internal_error")
            return {
                "status": _ERROR_CODE_TO_TOOL_STATUS.get(error_code, "internal_error"),
                "operation": "submit_prediction_experiment",
                "message": result.get("message") or "实验入队失败。",
                "data": None,
            }
        summary = result.get("result_summary") or {}
        experiment_id = str(summary.get("experiment_id") or "")
        return {
            "status": "queued",
            "operation": "submit_prediction_experiment",
            "data": {
                **_experiment_summary(prepared),
                "experiment_id": experiment_id,
                "job_status": "pending",
                "execution": _executor_hint(experiment_id),
                "note": "已入队（pending）；平台执行器自动领取执行（分钟级启动），稍后用 get_experiment_run 查询。",
            },
        }

    return await _execute_service_operation(
        operation_name="submit_prediction_experiment",
        operation=operation,
    )


@tool(args_schema=SubmitPredictionExperimentInput)
async def cancel_prediction_experiment_submission(
    confirmation_id: str,
    *,
    config: RunnableConfig,
) -> dict[str, Any]:
    """撤销尚未入队的预测实验确认；不会取消已经提交的实验。"""

    thread_id, user_id = _confirmation_scope(config)
    revoked = await confirmation_store.arevoke(
        confirmation_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    if not revoked:
        return {
            "status": "invalid_confirmation",
            "operation": "cancel_prediction_experiment_submission",
            "message": "确认 ID 无效、已过期、已使用，或不属于当前对话。",
            "data": None,
        }
    return {
        "status": "cancelled",
        "operation": "cancel_prediction_experiment_submission",
        "message": "预测实验准备已取消，确认 ID 已撤销。",
        "data": None,
    }


@tool(args_schema=GetExperimentRunInput)
async def get_experiment_run(experiment_id: str) -> dict[str, Any]:
    """查询自研预测器实验的状态、覆盖与六档指标（实验口径 `m6-eval-v1`）。

    口径提醒：这是**自研预测器批量评估**（基于实飞观测外推）；与 SCAT 原始预测
    评估（CFPL 计划 vs 实飞）和任务模型（GBAR）评估是不同体系，数字不得混列；
    对比不同实验时必须同时给出预测器版本与样本量。

    适用问题：
    - 刚才提交的实验跑到哪了（pending/running/succeeded）；
    - 实验的六档 MAE/中位数/P90/P95、覆盖航班数与最差样本；
    - 两个预测器在同一批次上的指标对比（各自带版本与样本量）。
    """
    normalized_id = str(experiment_id).strip()

    async def operation() -> dict[str, Any]:
        repository = ExperimentRepository(flight_database.db)
        document = await repository.get(normalized_id)
        if document is None:
            return {
                "status": "not_found",
                "operation": "get_experiment_run",
                "query": {"experiment_id": normalized_id},
                "message": f"未找到实验 {normalized_id}。",
                "data": None,
            }
        document.pop("_id", None)
        return {
            "status": "ok",
            "operation": "get_experiment_run",
            "query": {"experiment_id": normalized_id},
            "data": {
                "experiment_id": document.get("experiment_id"),
                "status": document.get("status"),
                "batch": document.get("batch"),
                "predictor": {
                    "id": document.get("predictor_id"),
                    "version": document.get("predictor_version"),
                },
                "model_version": document.get("model_version"),
                "data_version": document.get("data_version"),
                "feature_version": document.get("feature_version"),
                "protocol_version": document.get("protocol_version"),
                "hyperparameters": document.get("hyperparameters") or {},
                "config": document.get("config") or {},
                "dataset": document.get("dataset") or {},
                "coverage": document.get("coverage") or {},
                "metrics": document.get("metrics") or [],
                "phase_metrics": document.get("phase_metrics") or [],
                "worst_samples": (document.get("worst_samples") or [])[:5],
                "timing": document.get("timing") or {},
                "applicability": document.get("applicability"),
                "artifacts": document.get("artifacts") or [],
                "source": document.get("source"),
                "message": document.get("message"),
                "attempt_count": document.get("attempt_count", 0),
                "max_attempts": document.get("max_attempts"),
                "next_attempt_at": document.get("next_attempt_at"),
                "requeue_count": document.get("requeue_count", 0),
                "created_at": document.get("created_at"),
                "finished_at": document.get("finished_at"),
            },
        }

    return await _execute_service_operation(
        operation_name="get_experiment_run",
        operation=operation,
    )


@tool(args_schema=ListPredictionExperimentsInput)
async def list_prediction_experiments(
    batch: str | None = None,
    predictor_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """列出最近的自研预测器实验（摘要：状态、预测器版本、时间），按创建时间倒序。

    适用问题：有哪些实验、分别是什么状态、最近跑过哪些预测器。
    摘要不含指标；指标详情用 get_experiment_run。
    """

    async def operation() -> dict[str, Any]:
        repository = ExperimentRepository(flight_database.db)
        rows = await repository.list_recent(
            batch=batch,
            predictor_id=predictor_id,
            limit=limit,
        )
        return {
            "status": "ok",
            "operation": "list_prediction_experiments",
            "query": {"batch": batch, "predictor_id": predictor_id, "limit": limit},
            "data": {"count": len(rows), "experiments": rows},
        }

    return await _execute_service_operation(
        operation_name="list_prediction_experiments",
        operation=operation,
    )


experiment_tools: list[BaseTool] = [
    prepare_prediction_experiment,
    submit_prediction_experiment,
    cancel_prediction_experiment_submission,
    get_experiment_run,
    list_prediction_experiments,
]

__all__ = [
    "CONFIRMATION_TTL",
    "ExperimentConfirmationStore",
    "PreparedPredictionExperiment",
    "confirmation_store",
    "cancel_prediction_experiment_submission",
    "experiment_tools",
    "get_experiment_run",
    "list_prediction_experiments",
    "prepare_prediction_experiment",
    "submit_prediction_experiment",
]
