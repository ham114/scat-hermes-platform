"""M4 物化任务编排：任务记录、进度、失败原因与重试（M4-06/M4-07）。

任务生命周期（scat_materialization_jobs，见 contracts.MaterializationJobRecord）：

1. 开始前写入 `running` 记录（total_count 已知时一并写入）；
2. 处理过程中按间隔刷新 `progress` / `processed_count`，进程被杀后记录停留在
   `running`，重跑时按增量补齐剩余单位（任务可从中断位置继续）；
3. 结束时写入 `succeeded` / `partial` / `failed`，单个单位失败只进入
   `failed_targets` + `failure_detail`，不丢弃其它已写入的结果；
4. 重试（retry_of）只重算上一任务失败的单位，并在新记录上标注来源任务。

增量口径（M4-06）：SCAT 原始文档导入后不可变，因此「增量」= 跳过已有相同
`source_version + algorithm_version` 结果的单位；算法版本或数据版本递增时版本键
变化，全部单位重算，旧版本结果不被覆盖。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

from analysis_runs.artifact_contracts import slug_segment
from core.observability import log_alert, log_event
from scat.materialization.contracts import (
    MaterializationJobRecord,
    MaterializationJobStatus,
)
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    BATCH_STATISTICS_ALGORITHM_VERSION,
    DEFAULT_SOURCE_VERSION,
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    PREDICTION_METRICS_ALGORITHM_VERSION,
    materialize_batch_flight_features,
    materialize_batch_flight_phase_segments,
    materialize_batch_prediction_metrics,
    materialize_batch_statistics,
    materialize_prediction_metric_summaries,
)
from scat.repositories.flight_repository import ScatFlightRepository

# 进度写入间隔：太小放大写入量，太大则在中断时丢失进度。
DEFAULT_PROGRESS_INTERVAL: Final[int] = 50
_MAX_ERROR_LENGTH: Final[int] = 500
_MAX_FAILURE_DETAIL: Final[int] = 200
logger = logging.getLogger("scat.materialization.task")


@dataclass(slots=True)
class MaterializationJobOutcome:
    """任务执行结果（CLI 输出与测试断言使用）。"""

    job_id: str
    kind: str
    batch: str
    status: MaterializationJobStatus
    total_count: int
    processed_count: int
    succeeded_count: int
    skipped_count: int
    failed: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "batch": self.batch,
            "status": self.status,
            "total_count": self.total_count,
            "processed_count": self.processed_count,
            "succeeded_count": self.succeeded_count,
            "skipped_count": self.skipped_count,
            "failed_count": len(self.failed),
            "failed": self.failed[:100],
        }


def new_job_id(kind: str, batch: str, *, now: datetime | None = None) -> str:
    """生成可读任务 ID：`种类-批次-时间-短后缀`（同参数重复运行仍唯一）。"""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S")
    return f"{slug_segment(kind, 24)}-{slug_segment(batch, 60)}-{stamp}-{uuid4().hex[:6]}"


async def run_flight_features_job(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    force: bool = False,
    limit: int | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    job_id: str | None = None,
    retry_of: str | None = None,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_FEATURES_ALGORITHM_VERSION,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
    generated_at: datetime | None = None,
    on_progress: Callable[[MaterializationJobRecord], Awaitable[None]] | None = None,
) -> MaterializationJobOutcome:
    """执行航班特征物化任务并维护任务记录（M4-05/M4-06/M4-07）。

    - `external_ids` 指定时只处理这些航班（单航班/子集重算）；
    - `retry_of` 指定时取该历史任务的失败单位作为处理范围；
    - `force=False`（默认）按增量跳过已有同版本结果的航班，`force=True` 强制重算；
    - `on_progress` 每次写入任务记录后回调（CLI 用来输出进度）。
    """
    return await _run_batch_job(
        kind="flight_features",
        batch_fn=materialize_batch_flight_features,
        batch=batch,
        flight_repository=flight_repository,
        materialization_repository=materialization_repository,
        external_ids=external_ids,
        force=force,
        limit=limit,
        page_size=page_size,
        concurrency=concurrency,
        job_id=job_id,
        retry_of=retry_of,
        source_version=source_version,
        algorithm_version=algorithm_version,
        progress_interval=progress_interval,
        generated_at=generated_at,
        on_progress=on_progress,
    )


async def run_flight_phase_segments_job(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    force: bool = False,
    limit: int | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    job_id: str | None = None,
    retry_of: str | None = None,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
    generated_at: datetime | None = None,
    on_progress: Callable[[MaterializationJobRecord], Awaitable[None]] | None = None,
) -> MaterializationJobOutcome:
    """执行段级阶段标注物化任务并维护任务记录（M4-12，复用 M4-07 编排）。"""
    return await _run_batch_job(
        kind="phase_segments",
        batch_fn=materialize_batch_flight_phase_segments,
        batch=batch,
        flight_repository=flight_repository,
        materialization_repository=materialization_repository,
        external_ids=external_ids,
        force=force,
        limit=limit,
        page_size=page_size,
        concurrency=concurrency,
        job_id=job_id,
        retry_of=retry_of,
        source_version=source_version,
        algorithm_version=algorithm_version,
        progress_interval=progress_interval,
        generated_at=generated_at,
        on_progress=on_progress,
    )


async def run_prediction_metrics_job(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    force: bool = False,
    limit: int | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    job_id: str | None = None,
    retry_of: str | None = None,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = PREDICTION_METRICS_ALGORITHM_VERSION,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
    generated_at: datetime | None = None,
    on_progress: Callable[[MaterializationJobRecord], Awaitable[None]] | None = None,
) -> MaterializationJobOutcome:
    """执行预测指标物化任务并维护任务记录（M5-05，复用 M4-07 编排）。"""
    return await _run_batch_job(
        kind="prediction_metrics",
        batch_fn=materialize_batch_prediction_metrics,
        batch=batch,
        flight_repository=flight_repository,
        materialization_repository=materialization_repository,
        external_ids=external_ids,
        force=force,
        limit=limit,
        page_size=page_size,
        concurrency=concurrency,
        job_id=job_id,
        retry_of=retry_of,
        source_version=source_version,
        algorithm_version=algorithm_version,
        progress_interval=progress_interval,
        generated_at=generated_at,
        on_progress=on_progress,
    )


async def _run_batch_job(
    *,
    kind: str,
    batch_fn: Callable[..., Awaitable[dict[str, Any]]],
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    external_ids: Sequence[str | int] | None = None,
    force: bool = False,
    limit: int | None = None,
    page_size: int = 200,
    concurrency: int = 4,
    job_id: str | None = None,
    retry_of: str | None = None,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = FLIGHT_FEATURES_ALGORITHM_VERSION,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
    generated_at: datetime | None = None,
    on_progress: Callable[[MaterializationJobRecord], Awaitable[None]] | None = None,
) -> MaterializationJobOutcome:
    """单位级物化任务的通用实现（M4-07）：进度、失败单位与重试、版本隔离。

    - `external_ids` 指定时只处理这些单位（单航班/子集重算）；
    - `retry_of` 指定时取该历史任务的失败单位作为处理范围；
    - `force=False`（默认）按增量跳过已有同版本结果的单位，`force=True` 强制重算；
    - `on_progress` 每次写入任务记录后回调（CLI 用来输出进度）。
    """
    generated = generated_at or datetime.now(UTC)
    resolved_job_id = job_id or new_job_id(kind, batch, now=generated)
    task_started_at = datetime.now(UTC)

    targets: list[str] | None = (
        [str(value) for value in external_ids] if external_ids is not None else None
    )
    if targets is None and retry_of is not None:
        targets = await failed_targets_of(
            materialization_repository,
            retry_of,
            kind=kind,
        )

    existing = await materialization_repository.existing_values(
        kind,
        "external_id",
        {
            "batch": batch,
            "source_version": source_version,
            "algorithm_version": algorithm_version,
        },
    )

    if targets is not None:
        unique_targets = list(dict.fromkeys(targets))
        pending = (
            unique_targets
            if force
            else [value for value in unique_targets if value not in existing]
        )
        total_count = len(pending)
    else:
        batch_total = await flight_repository.count_all(batch=batch)
        pending_total = batch_total if force else max(batch_total - len(existing), 0)
        total_count = pending_total
    if limit is not None:
        total_count = min(total_count, limit)

    record = MaterializationJobRecord(
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated,
        job_id=resolved_job_id,
        kind=kind,
        batch=batch,
        status="running",
        progress=0.0,
        total_count=total_count,
        retry_of=retry_of,
    )
    await materialization_repository.upsert_job(record)
    log_event(
        logger,
        logging.INFO,
        "materialization_task_started",
        task_id=resolved_job_id,
        task_kind=kind,
        batch=batch,
        total_count=total_count,
        retry=retry_of is not None,
    )

    attempted = 0
    failed: list[dict[str, str]] = []

    async def refresh(
        *,
        status: MaterializationJobStatus,
        skipped: int = 0,
        error_message: str | None = None,
    ) -> None:
        record.status = status
        record.processed_count = attempted
        record.failed_targets = [item["external_id"] for item in failed]
        record.failure_detail = {
            item["external_id"]: item["error"] for item in failed[:_MAX_FAILURE_DETAIL]
        }
        record.error_message = error_message
        record.progress = min((attempted + skipped) / total_count, 1.0) if total_count > 0 else 1.0
        await materialization_repository.upsert_job(record)
        elapsed_s = max((datetime.now(UTC) - task_started_at).total_seconds(), 0.001)
        log_event(
            logger,
            logging.INFO
            if status == "running"
            else logging.WARNING
            if status == "partial"
            else logging.ERROR
            if status == "failed"
            else logging.INFO,
            "materialization_task_progress"
            if status == "running"
            else "materialization_task_completed",
            task_id=resolved_job_id,
            task_kind=kind,
            batch=batch,
            status=status,
            processed_count=attempted,
            total_count=total_count,
            failed_count=len(failed),
            progress=round(record.progress, 4),
            throughput_per_second=round(attempted / elapsed_s, 3),
            duration_s=round(elapsed_s, 3),
        )
        if status in {"failed", "partial"}:
            log_alert(
                logger,
                "materialization_task_failure",
                task_id=resolved_job_id,
                task_kind=kind,
                batch=batch,
                status=status,
                failed_count=len(failed),
            )
        if on_progress is not None:
            await on_progress(record)

    async def handle_progress(update: dict[str, Any]) -> None:
        nonlocal attempted
        attempted = int(update["processed"])
        failure = update.get("failure")
        if isinstance(failure, dict):
            failed.append(
                {
                    "external_id": str(failure.get("external_id", "")),
                    "error": str(failure.get("error", ""))[:300],
                }
            )
        if attempted % max(progress_interval, 1) == 0:
            await refresh(status="running")

    try:
        result = await batch_fn(
            batch=batch,
            flight_repository=flight_repository,
            materialization_repository=materialization_repository,
            external_ids=targets,
            skip_existing=not force,
            source_version=source_version,
            algorithm_version=algorithm_version,
            generated_at=generated,
            page_size=page_size,
            concurrency=concurrency,
            limit=limit,
            on_progress=handle_progress,
        )
    except Exception as error:
        await refresh(status="failed", error_message=str(error)[:_MAX_ERROR_LENGTH])
        raise

    if failed and int(result["succeeded_count"]) == 0:
        status: MaterializationJobStatus = "failed"
    elif failed:
        status = "partial"
    else:
        status = "succeeded"
    await refresh(status=status, skipped=int(result["skipped_count"]))

    return MaterializationJobOutcome(
        job_id=resolved_job_id,
        kind=kind,
        batch=batch,
        status=status,
        total_count=total_count,
        processed_count=attempted,
        succeeded_count=int(result["succeeded_count"]),
        skipped_count=int(result["skipped_count"]),
        failed=failed,
    )


async def run_batch_statistics_job(
    *,
    batch: str,
    flight_repository: ScatFlightRepository,
    materialization_repository: MaterializationRepository,
    job_id: str | None = None,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = BATCH_STATISTICS_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    on_progress: Callable[[MaterializationJobRecord], Awaitable[None]] | None = None,
) -> MaterializationJobOutcome:
    """执行批次统计物化任务并维护任务记录（M4-04/M4-07）。"""
    generated = generated_at or datetime.now(UTC)
    resolved_job_id = job_id or new_job_id("batch_statistics", batch, now=generated)
    task_started_at = datetime.now(UTC)
    record = MaterializationJobRecord(
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated,
        job_id=resolved_job_id,
        kind="batch_statistics",
        batch=batch,
        status="running",
        progress=0.0,
        total_count=1,
    )
    await materialization_repository.upsert_job(record)
    log_event(
        logger,
        logging.INFO,
        "materialization_task_started",
        task_id=resolved_job_id,
        task_kind="batch_statistics",
        batch=batch,
        total_count=1,
    )
    if on_progress is not None:
        await on_progress(record)

    try:
        await materialize_batch_statistics(
            batch=batch,
            flight_repository=flight_repository,
            materialization_repository=materialization_repository,
            source_version=source_version,
            algorithm_version=algorithm_version,
            generated_at=generated,
        )
    except Exception as error:
        record.status = "failed"
        record.error_message = str(error)[:_MAX_ERROR_LENGTH]
        await materialization_repository.upsert_job(record)
        if on_progress is not None:
            await on_progress(record)
        log_alert(
            logger,
            "materialization_task_failure",
            task_id=resolved_job_id,
            task_kind="batch_statistics",
            batch=batch,
            status="failed",
            failure_type=type(error).__name__,
        )
        raise

    record.status = "succeeded"
    record.processed_count = 1
    record.progress = 1.0
    await materialization_repository.upsert_job(record)
    if on_progress is not None:
        await on_progress(record)
    duration_s = max((datetime.now(UTC) - task_started_at).total_seconds(), 0.001)
    log_event(
        logger,
        logging.INFO,
        "materialization_task_completed",
        task_id=resolved_job_id,
        task_kind="batch_statistics",
        batch=batch,
        status="succeeded",
        processed_count=1,
        total_count=1,
        throughput_per_second=round(1 / duration_s, 3),
        duration_s=round(duration_s, 3),
    )
    return MaterializationJobOutcome(
        job_id=resolved_job_id,
        kind="batch_statistics",
        batch=batch,
        status="succeeded",
        total_count=1,
        processed_count=1,
        succeeded_count=1,
        skipped_count=0,
    )


async def run_prediction_metric_summaries_job(
    *,
    batch: str,
    materialization_repository: MaterializationRepository,
    job_id: str | None = None,
    source_version: str = DEFAULT_SOURCE_VERSION,
    algorithm_version: str = PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    generated_at: datetime | None = None,
    on_progress: Callable[[MaterializationJobRecord], Awaitable[None]] | None = None,
) -> MaterializationJobOutcome:
    """执行指标汇总任务（M5-06，批次级：一次作业只处理一个批次）。"""
    generated = generated_at or datetime.now(UTC)
    resolved_job_id = job_id or new_job_id("prediction_metric_summaries", batch, now=generated)
    task_started_at = datetime.now(UTC)
    record = MaterializationJobRecord(
        source_version=source_version,
        algorithm_version=algorithm_version,
        generated_at=generated,
        job_id=resolved_job_id,
        kind="prediction_metric_summaries",
        batch=batch,
        status="running",
        progress=0.0,
        total_count=1,
    )
    await materialization_repository.upsert_job(record)
    log_event(
        logger,
        logging.INFO,
        "materialization_task_started",
        task_id=resolved_job_id,
        task_kind="prediction_metric_summaries",
        batch=batch,
        total_count=1,
    )
    if on_progress is not None:
        await on_progress(record)

    try:
        written = await materialize_prediction_metric_summaries(
            batch=batch,
            materialization_repository=materialization_repository,
            source_version=source_version,
            algorithm_version=algorithm_version,
            generated_at=generated,
        )
    except Exception as error:
        record.status = "failed"
        record.error_message = str(error)[:_MAX_ERROR_LENGTH]
        await materialization_repository.upsert_job(record)
        if on_progress is not None:
            await on_progress(record)
        log_alert(
            logger,
            "materialization_task_failure",
            task_id=resolved_job_id,
            task_kind="prediction_metric_summaries",
            batch=batch,
            status="failed",
            failure_type=type(error).__name__,
        )
        raise

    record.status = "succeeded"
    record.processed_count = 1
    record.progress = 1.0
    record.failure_detail = {"summary_records": str(written)}
    await materialization_repository.upsert_job(record)
    if on_progress is not None:
        await on_progress(record)
    duration_s = max((datetime.now(UTC) - task_started_at).total_seconds(), 0.001)
    log_event(
        logger,
        logging.INFO,
        "materialization_task_completed",
        task_id=resolved_job_id,
        task_kind="prediction_metric_summaries",
        batch=batch,
        status="succeeded",
        processed_count=1,
        total_count=1,
        output_count=written,
        throughput_per_second=round(1 / duration_s, 3),
        duration_s=round(duration_s, 3),
    )
    return MaterializationJobOutcome(
        job_id=resolved_job_id,
        kind="prediction_metric_summaries",
        batch=batch,
        status="succeeded",
        total_count=1,
        processed_count=1,
        succeeded_count=1,
        skipped_count=0,
    )


async def failed_targets_of(
    materialization_repository: MaterializationRepository,
    job_id: str,
    *,
    kind: str | None = None,
) -> list[str]:
    """读取历史任务的失败单位用于重试；任务不存在或类型不符时明确报错。"""
    document = await materialization_repository.find_job(job_id)
    if document is None:
        raise ValueError(f"任务不存在: {job_id}")
    if kind is not None and document.get("kind") != kind:
        raise ValueError(f"任务 {job_id} 的类型是 {document.get('kind')}，不能用于 {kind} 重试")
    return [str(value) for value in document.get("failed_targets") or []]


__all__ = [
    "DEFAULT_PROGRESS_INTERVAL",
    "MaterializationJobOutcome",
    "failed_targets_of",
    "new_job_id",
    "run_batch_statistics_job",
    "run_flight_features_job",
    "run_flight_phase_segments_job",
    "run_prediction_metric_summaries_job",
    "run_prediction_metrics_job",
]
