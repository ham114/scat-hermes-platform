"""M5-10 对话侧物化能力：批次任务化边界工具（prepare/submit/status）。

设计约束（计划 M5-10 / §5.3 子代理接入规范）：

- **批次**：禁止在对话里同步跑整批，也禁止 LLM 逐航班循环——只允许 prepare → 用户确认 →
  submit（仅写一条 `status=pending` 作业），实际执行由确定性作业执行器（CLI `--job-id`）领取；
- **确认 ID**：与完整参数绑定、15 分钟有效、单次消费、按 thread/user 作用域校验（与 MA-05 同模式）；
- 本模块只做参数校验、影响面统计与入队，不执行任何物化计算；
- 评估读取工具（单航班分段、最差案例、指标汇总）已迁至 `agents.eval_tools`（评估域）。

工具只注册给 `scat-flight-agent`（见 `agents.scat_agent.aviation_tools`）。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import TypeAdapter

from agents.scat_tools import _execute_service_operation
from core.redis_confirmation import redis_confirmation_store
from scat.infrastructure.database import flight_database
from scat.materialization.contracts import MaterializationJobRecord
from scat.materialization.jobs import new_job_id
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    DEFAULT_SOURCE_VERSION,
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
)
from scat.repositories.flight_repository import ScatFlightRepository
from scat.schemas.tool_inputs import (
    DEFAULT_BATCH,
    ListMaterializationJobsInput,
    MaterializationJobPrepareInput,
    MaterializationJobStatusInput,
    MaterializationJobSubmitInput,
)
from scat.services.materialization_service import MaterializationService

# 物化种类 → 当前算法版本（与 scat.materialization.tasks 保持一致）。
JOB_ALGORITHM_VERSIONS: dict[str, str] = {
    "flight_features": FLIGHT_FEATURES_ALGORITHM_VERSION,
    "phase_segments": FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
}
JOB_KIND_LABELS: dict[str, str] = {
    "flight_features": "航班级阶段特征（爬升/巡航/下降秒数）",
    "phase_segments": "段级阶段标注（RDP 分段 + 六类）",
}
CONFIRMATION_TTL = timedelta(minutes=15)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedMaterializationJob:
    """已准备（未入队）的物化任务：参数与影响面完整绑定在确认 ID 上。"""

    kind: str
    batch: str
    source_version: str
    algorithm_version: str
    force: bool
    limit: int | None
    total_count: int
    pending_count: int


_PREPARED_JOB_ADAPTER = TypeAdapter(PreparedMaterializationJob)


class MaterializationJobConfirmationStore:
    """进程内、可过期、一次性消费的任务确认（与 MA-05 的 ModelTaskConfirmationStore 同语义）。

    条目把「已准备好的任务参数」与确认 ID 绑定，提交时只消费 ID、不重放参数，
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
            str, tuple[PreparedMaterializationJob, datetime, str | None, str | None]
        ] = {}

    async def aissue(
        self,
        job: PreparedMaterializationJob,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, datetime]:
        if self._shared:
            shared = await redis_confirmation_store.issue(
                "materialization",
                asdict(job),
                ttl=self._ttl,
                thread_id=thread_id,
                user_id=user_id,
            )
            if shared is not None:
                return shared
        return self.issue(job, thread_id=thread_id, user_id=user_id)

    async def aconsume(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> PreparedMaterializationJob | None:
        if self._shared:
            payload = await redis_confirmation_store.consume(
                "materialization",
                confirmation_id,
                thread_id=thread_id,
                user_id=user_id,
            )
            if payload is not None:
                try:
                    return _PREPARED_JOB_ADAPTER.validate_python(payload)
                except Exception:  # noqa: BLE001 - corrupted optional cache entry
                    logger.warning("Invalid materialization confirmation payload in Redis")
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
            "materialization",
            confirmation_id,
            thread_id=thread_id,
            user_id=user_id,
        ):
            return True
        return self.revoke(confirmation_id, thread_id=thread_id, user_id=user_id)

    def issue(
        self,
        job: PreparedMaterializationJob,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        confirmation_id = uuid4().hex
        expires_at = now + self._ttl
        with self._lock:
            self._prune(now)
            self._pending[confirmation_id] = (job, expires_at, thread_id, user_id)
        return confirmation_id, expires_at

    def consume(
        self,
        confirmation_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> PreparedMaterializationJob | None:
        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            entry = self._pending.get(confirmation_id)
            if entry is None:
                return None
            job, _expires_at, entry_thread, entry_user = entry
            if entry_thread != thread_id or entry_user != user_id:
                return None
            self._pending.pop(confirmation_id, None)
            return job

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
            _job, _expires_at, entry_thread, entry_user = entry
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


confirmation_store = MaterializationJobConfirmationStore(shared=True)


def _confirmation_scope(config: RunnableConfig) -> tuple[str | None, str | None]:
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id")
    user_id = configurable.get("user_id")
    return (
        thread_id if isinstance(thread_id, str) else None,
        user_id if isinstance(user_id, str) else None,
    )


def _job_summary(job: PreparedMaterializationJob) -> dict[str, Any]:
    return {
        "kind": job.kind,
        "kind_label": JOB_KIND_LABELS.get(job.kind, job.kind),
        "batch": job.batch,
        "source_version": job.source_version,
        "algorithm_version": job.algorithm_version,
        "force": job.force,
        "limit": job.limit,
        "batch_flight_count": job.total_count,
        "pending_flight_count": job.pending_count,
    }


def _executor_hint(job: PreparedMaterializationJob, job_id: str) -> str:
    command = "phase-segments" if job.kind == "phase_segments" else "flight-features"
    return (
        "由确定性作业执行器领取执行（运算不交给对话模型）："
        f"PYTHONPATH=src python -m scat.materialization.cli {command} "
        f"--batch {job.batch} --job-id {job_id}"
    )


@tool(args_schema=MaterializationJobPrepareInput)
async def prepare_materialization_job(
    kind: Literal["flight_features", "phase_segments"] = "phase_segments",
    batch: str = DEFAULT_BATCH,
    force: bool = False,
    limit: int | None = None,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """准备一个批次物化任务（阶段标注/阶段特征），返回参数摘要、影响面与一次性确认 ID。

    **不写数据库、不执行计算**：只统计"该批次共多少航班、其中多少还没有当前版本结果"。
    拿到 confirmation_id 后，必须先向用户明确说明将要执行的内容并取得确认，再调用
    `submit_materialization_job(confirmation_id)` 入队；确认 ID 15 分钟有效、只能用一次。

    批次的真实计算由运维侧作业执行器完成（对话不会同步跑整批）。
    """
    if kind not in JOB_ALGORITHM_VERSIONS:
        return {
            "status": "invalid_request",
            "operation": "prepare_materialization_job",
            "message": f"未知物化种类: {kind}",
            "data": None,
        }
    algorithm_version = JOB_ALGORITHM_VERSIONS[kind]

    async def operation() -> dict[str, Any]:
        materialization_repository = MaterializationRepository(flight_database.db)
        flight_repository = ScatFlightRepository()
        total_count = await flight_repository.count_all(batch=batch)
        existing = await materialization_repository.existing_values(
            kind,
            "external_id",
            {
                "batch": batch,
                "source_version": DEFAULT_SOURCE_VERSION,
                "algorithm_version": algorithm_version,
            },
        )
        pending_count = total_count if force else max(total_count - len(existing), 0)
        if limit is not None:
            pending_count = min(pending_count, limit)
        job = PreparedMaterializationJob(
            kind=kind,
            batch=batch,
            source_version=DEFAULT_SOURCE_VERSION,
            algorithm_version=algorithm_version,
            force=force,
            limit=limit,
            total_count=total_count,
            pending_count=pending_count,
        )
        thread_id, user_id = _confirmation_scope(config or {})
        confirmation_id, expires_at = await confirmation_store.aissue(
            job,
            thread_id=thread_id,
            user_id=user_id,
        )
        return {
            "status": "awaiting_confirmation",
            "operation": "prepare_materialization_job",
            "confirmation_id": confirmation_id,
            "expires_at": expires_at.isoformat(),
            "data": {
                **_job_summary(job),
                "note": "尚未入队；请向用户确认后再调用 submit_materialization_job。",
            },
        }

    return await _execute_service_operation(
        operation_name="prepare_materialization_job",
        operation=operation,
    )


@tool(args_schema=MaterializationJobSubmitInput)
async def submit_materialization_job(
    confirmation_id: str,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """提交（入队）已准备的批次物化任务：只在作业集合写一条 pending 作业。

    - 只接受 `prepare_materialization_job` 返回的确认 ID（15 分钟有效、单次消费）；
    - **不执行计算**：入队后由作业执行器领取（CLI `--job-id`）；
    - 提交后可用 `get_materialization_job(job_id)` 查询进度与失败单位。

    绝不允许由模型直接指定批次参数绕过 prepare/确认流程。
    """
    thread_id, user_id = _confirmation_scope(config or {})
    job = await confirmation_store.aconsume(
        confirmation_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    if job is None:
        return {
            "status": "invalid_confirmation",
            "operation": "submit_materialization_job",
            "message": "确认 ID 无效、已过期或已被使用；请重新调用 prepare_materialization_job。",
            "data": None,
        }

    async def operation() -> dict[str, Any]:
        repository = MaterializationRepository(flight_database.db)
        record = MaterializationJobRecord(
            source_version=job.source_version,
            algorithm_version=job.algorithm_version,
            generated_at=datetime.now(UTC),
            job_id=new_job_id(job.kind, job.batch),
            kind=job.kind,
            batch=job.batch,
            status="pending",
            progress=0.0,
            total_count=job.pending_count,
        )
        await repository.upsert_job(record)
        return {
            "status": "queued",
            "operation": "submit_materialization_job",
            "data": {
                **_job_summary(job),
                "job_id": record.job_id,
                "job_status": record.status,
                "execution": _executor_hint(job, record.job_id),
            },
        }

    return await _execute_service_operation(
        operation_name="submit_materialization_job",
        operation=operation,
    )


@tool(args_schema=MaterializationJobSubmitInput)
async def cancel_materialization_job_submission(
    confirmation_id: str,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """撤销尚未入队的物化任务确认；不会取消已经提交的运行中作业。"""

    thread_id, user_id = _confirmation_scope(config or {})
    revoked = await confirmation_store.arevoke(
        confirmation_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    if not revoked:
        return {
            "status": "invalid_confirmation",
            "operation": "cancel_materialization_job_submission",
            "message": "确认 ID 无效、已过期、已使用，或不属于当前对话。",
            "data": None,
        }
    return {
        "status": "cancelled",
        "operation": "cancel_materialization_job_submission",
        "message": "物化任务准备已取消，确认 ID 已撤销。",
        "data": None,
    }


@tool(args_schema=MaterializationJobStatusInput)
async def get_materialization_job(job_id: str) -> dict[str, Any]:
    """查询物化任务的进度、失败单位与错误原因。

    适用问题：刚才那个批次任务跑到哪了；有哪些航班失败了、能不能重试。
    """
    normalized_job_id = str(job_id).strip()

    async def operation() -> dict[str, Any]:
        service = MaterializationService()
        job = await service.get_job(normalized_job_id)
        return {
            "status": "ok",
            "operation": "get_materialization_job",
            "query": {"job_id": normalized_job_id},
            "data": job,
        }

    return await _execute_service_operation(
        operation_name="get_materialization_job",
        operation=operation,
    )


@tool(args_schema=ListMaterializationJobsInput)
async def list_materialization_jobs(
    kind: str | None = None,
    batch: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """列出最近的物化任务及状态、进度、失败数量和重试来源。"""

    async def operation() -> dict[str, Any]:
        service = MaterializationService()
        return await service.list_jobs(
            kind=kind,
            batch=batch,
            status_filter=status,
            limit=limit,
        )

    return await _execute_service_operation(
        operation_name="list_materialization_jobs",
        operation=operation,
    )


materialization_tools: list[BaseTool] = [
    prepare_materialization_job,
    submit_materialization_job,
    cancel_materialization_job_submission,
    get_materialization_job,
    list_materialization_jobs,
]

__all__ = [
    "CONFIRMATION_TTL",
    "JOB_ALGORITHM_VERSIONS",
    "JOB_KIND_LABELS",
    "MaterializationJobConfirmationStore",
    "PreparedMaterializationJob",
    "confirmation_store",
    "cancel_materialization_job_submission",
    "get_materialization_job",
    "list_materialization_jobs",
    "materialization_tools",
    "prepare_materialization_job",
    "submit_materialization_job",
]
