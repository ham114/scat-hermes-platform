"""M6 评估运行器：实验的创建、领取与执行（实验台账 `scat_model_experiments`）。

流程：

- CLI 直接运行：`run_predictor_evaluation` 建 running 记录 → 执行 → 终态；
- 对话提交（M6-08）：`create_pending_experiment` 只落 pending 记录；执行由确定性
  执行器领取（`run_pending_experiment`，经 CLI `claim`，D-19 模式）；
- 报告完整性（M6 验收）：效果（六档指标 + 阶段分组）、推理耗时（timing）、适用范围
  （applicability），并导出报告产物文件（`scat.experiments.artifacts`，sha256 登记）；
- 分组表现依赖物化段级标注（`scat_flight_phase_segments`）；缺失时降级跳过并计入
  `coverage.phase_grouping`，不阻塞整体评估；
- 单航班失败不阻断整批（计入 coverage.failed_flights，状态置 partial）；
- 只读原始数据（`get_evaluation_inputs`），只写实验集合与产物目录。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from typing import Any
from uuid import uuid4

from core import settings
from core.observability import log_alert, log_event
from scat.experiments.artifacts import ExperimentArtifactStore
from scat.experiments.contracts import ExperimentRecord
from scat.experiments.evaluation import (
    EvaluationConfig,
    SampleResult,
    aggregate_phase_metrics,
    aggregate_samples,
    evaluate_track,
)
from scat.experiments.grouping import phase_for_time
from scat.experiments.repository import ExperimentRepository
from scat.materialization.tasks import DEFAULT_SOURCE_VERSION, iter_batch_external_ids
from scat.prediction.alignment import (
    DEFAULT_MAX_TRACK_GAP_S,
    HORIZONS_S,
    build_track,
)
from scat.prediction.interfaces import PREDICTOR_SCHEMA_VERSION
from scat.prediction.registry import PredictorRegistry, default_registry
from scat.repositories.flight_repository import ScatFlightRepository
from scat.services.materialization_service import MaterializationService

__all__ = [
    "build_experiment_id",
    "build_report_payload",
    "create_pending_experiment",
    "ExperimentLeaseLostError",
    "export_experiment_report",
    "run_pending_experiment",
    "run_predictor_evaluation",
]

_HYPERPARAMETER_FIELDS = ("window_s", "process_noise_mps2", "measurement_noise_m")
REPORT_ARTIFACT_NAME = "evaluation_report.json"
logger = logging.getLogger("scat.experiment.task")

PhaseLookup = Callable[[str], Awaitable[list[dict[str, Any]] | None]]


class ExperimentLeaseLostError(RuntimeError):
    """当前执行器无法再证明自己持有实验租约。"""


def _default_lease_owner() -> str:
    """生成便于运维定位、但不承担鉴权职责的 worker 标识。"""
    return f"{socket.gethostname()}:{os.getpid()}"


async def _maintain_lease(
    *,
    experiment_repository: ExperimentRepository,
    experiment_id: str,
    lease_token: str,
    lease_ttl: timedelta,
    heartbeat_interval_seconds: float,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    """独立于进度回调周期续租；任何续租失败都视为租约已失效。"""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=heartbeat_interval_seconds)
        except TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            renewed = await experiment_repository.heartbeat(
                experiment_id,
                lease_token=lease_token,
                lease_ttl=lease_ttl,
            )
        except Exception as error:  # noqa: BLE001 - 无法确认续租成功时必须按失租处理
            log_alert(
                logger,
                "experiment_lease_heartbeat_failure",
                task_id=experiment_id,
                failure_type=type(error).__name__,
            )
            lost.set()
            return
        if not renewed:
            log_alert(
                logger,
                "experiment_lease_lost",
                task_id=experiment_id,
            )
            lost.set()
            return


def build_experiment_id(predictor_id: str, batch: str, started: datetime) -> str:
    return f"m6eval-{predictor_id}-{batch}-{started:%Y%m%dT%H%M%S}-{uuid4().hex[:6]}"


def _hyperparameters(predictor: Any) -> dict[str, Any]:
    return {
        name: getattr(predictor, name)
        for name in _HYPERPARAMETER_FIELDS
        if hasattr(predictor, name)
    }


def _config_payload(config: EvaluationConfig, limit: int | None) -> dict[str, Any]:
    return {
        "horizons_s": list(config.horizons_s),
        "stride_s": config.stride_s,
        "max_cutoffs_per_flight": config.max_cutoffs_per_flight,
        "min_observed_s": config.min_observed_s,
        "min_observed_points": config.min_observed_points,
        "max_actual_gap_s": config.max_actual_gap_s,
        "limit": limit,
    }


def _config_from_record(record: ExperimentRecord) -> EvaluationConfig:
    config = record.config or {}
    return EvaluationConfig(
        batch=record.batch,
        predictor_id=record.predictor_id,
        horizons_s=tuple(float(value) for value in config.get("horizons_s") or HORIZONS_S),
        stride_s=float(config.get("stride_s", 60.0)),
        max_cutoffs_per_flight=int(config.get("max_cutoffs_per_flight", 12)),
        min_observed_s=float(config.get("min_observed_s", 60.0)),
        min_observed_points=int(config.get("min_observed_points", 3)),
        max_actual_gap_s=float(config.get("max_actual_gap_s", DEFAULT_MAX_TRACK_GAP_S)),
    )


def _build_record(
    *,
    config: EvaluationConfig,
    limit: int | None,
    predictor: Any,
    status: str,
    source: str,
    requested_by: str | None,
    created_at: datetime,
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=build_experiment_id(predictor.predictor_id, config.batch, created_at),
        status=status,
        batch=config.batch,
        predictor_id=predictor.predictor_id,
        predictor_version=predictor.predictor_version,
        model_version=predictor.predictor_version,
        data_version=DEFAULT_SOURCE_VERSION,
        feature_version=f"predictor-schema-v{PREDICTOR_SCHEMA_VERSION}",
        hyperparameters=_hyperparameters(predictor),
        applicability=getattr(predictor, "applicability_note", None),
        config=_config_payload(config, limit),
        dataset={},
        coverage={},
        source=source,
        requested_by=requested_by,
        max_attempts=int(settings.SCAT_EXPERIMENT_MAX_ATTEMPTS),
        code_version=settings.ANALYSIS_CODE_VERSION,
        created_at=created_at,
    )


async def create_pending_experiment(
    *,
    config: EvaluationConfig,
    experiment_repository: ExperimentRepository,
    limit: int | None = None,
    registry: PredictorRegistry | None = None,
    requested_by: str | None = None,
    source: str = "cli",
) -> ExperimentRecord:
    """只落一条 `pending` 实验记录（不执行计算）；未知预测器抛 ValueError。"""
    active_registry = registry if registry is not None else default_registry
    predictor = active_registry.get(config.predictor_id)
    record = _build_record(
        config=config,
        limit=limit,
        predictor=predictor,
        status="pending",
        source=source,
        requested_by=requested_by,
        created_at=datetime.now(UTC),
    )
    await experiment_repository.upsert(record)
    return record


@dataclass(slots=True)
class _TimingAccumulator:
    calls: int = 0
    seconds: float = 0.0


class _TimedPredictor:
    """给预测器套一层计时外壳（协议字段透传），在不改协议签名的前提下记录推理耗时。"""

    def __init__(self, inner: Any, accumulator: _TimingAccumulator) -> None:
        self._inner = inner
        self._accumulator = accumulator

    @property
    def predictor_id(self) -> str:
        return str(self._inner.predictor_id)

    @property
    def predictor_version(self) -> str:
        return str(self._inner.predictor_version)

    def predict(self, request: Any) -> Any:
        started = perf_counter()
        try:
            return self._inner.predict(request)
        finally:
            self._accumulator.calls += 1
            self._accumulator.seconds += perf_counter() - started


@dataclass(slots=True)
class _EvaluationOutcome:
    samples: list[SampleResult] = field(default_factory=list)
    exclusions: Counter[str] = field(default_factory=Counter)
    processed_flights: list[str] = field(default_factory=list)
    failed_flights: list[str] = field(default_factory=list)
    phase_samples: list[tuple[str, SampleResult]] = field(default_factory=list)
    grouping_flights_with_segments: int = 0
    grouping_flights_without_segments: int = 0
    timing: _TimingAccumulator = field(default_factory=_TimingAccumulator)


async def _materialized_phase_lookup(batch: str, external_id: str) -> list[dict[str, Any]] | None:
    """默认阶段查询：读取物化段级标注；任何失败降级为 None（分组是增强信息）。"""
    try:
        service = MaterializationService()
        document = await service.get_flight_phase_segments(batch=batch, external_id=external_id)
    except Exception:  # noqa: BLE001 - 分组降级不影响整体评估
        return None
    if not document:
        return None
    segments = document.get("segments") or []
    return list(segments) or None


async def _run_evaluation(
    *,
    config: EvaluationConfig,
    predictor: Any,
    limit: int | None,
    flight_repository: ScatFlightRepository,
    page_size: int,
    progress_interval: int,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None,
    phase_lookup: PhaseLookup | None = None,
    phase_grouping: bool = True,
) -> _EvaluationOutcome:
    outcome = _EvaluationOutcome()
    timed_predictor = _TimedPredictor(predictor, outcome.timing)
    async for external_id in iter_batch_external_ids(
        flight_repository,
        batch=config.batch,
        page_size=page_size,
    ):
        if limit is not None and len(outcome.processed_flights) >= limit:
            break
        outcome.processed_flights.append(external_id)
        try:
            inputs = await flight_repository.get_evaluation_inputs(
                batch=config.batch,
                external_id=external_id,
            )
        except Exception:  # noqa: BLE001 - 单航班失败不阻断整批（本地失败隔离）
            outcome.failed_flights.append(external_id)
            continue
        if not inputs:
            outcome.failed_flights.append(external_id)
            continue
        track, _duplicates = build_track(inputs.get("plots") or [])
        track_samples, track_exclusions = evaluate_track(
            track,
            timed_predictor,
            config,
            external_id=external_id,
        )
        outcome.samples.extend(track_samples)
        outcome.exclusions.update(track_exclusions)
        if phase_grouping and track_samples:
            segments = (
                await phase_lookup(external_id)
                if phase_lookup is not None
                else await _materialized_phase_lookup(config.batch, external_id)
            )
            if segments:
                outcome.grouping_flights_with_segments += 1
                for sample in track_samples:
                    phase = phase_for_time(segments, sample.cutoff_time)
                    if phase:
                        outcome.phase_samples.append((phase, sample))
            else:
                outcome.grouping_flights_without_segments += 1
        if (
            on_progress is not None
            and progress_interval > 0
            and len(outcome.processed_flights) % progress_interval == 0
        ):
            await on_progress(
                {
                    "processed": len(outcome.processed_flights),
                    "samples": len(outcome.samples),
                    "failed": len(outcome.failed_flights),
                }
            )
    return outcome


def _apply_outcome(
    record: ExperimentRecord,
    outcome: _EvaluationOutcome,
    *,
    config: EvaluationConfig,
    limit: int | None,
) -> None:
    metrics, worst_samples = aggregate_samples(outcome.samples, horizons=config.horizons_s)
    phase_by_sample = {id(sample): phase for phase, sample in outcome.phase_samples}
    phase_metrics, ungrouped_samples = aggregate_phase_metrics(
        outcome.samples,
        phase_of=lambda sample: phase_by_sample.get(id(sample)),
    )
    flights_with_samples = len({sample.external_id for sample in outcome.samples})
    ids_digest = (
        sha256(",".join(sorted(outcome.processed_flights)).encode("utf-8")).hexdigest()
        if outcome.processed_flights
        else None
    )
    record.dataset = {
        "selection": "batch_all",
        "limit": limit,
        "flight_count": len(outcome.processed_flights),
        "external_ids_sha256": ids_digest,
        "sample_flight_ids": outcome.processed_flights[:20],
    }
    record.coverage = {
        "flights_processed": len(outcome.processed_flights),
        "flights_with_samples": flights_with_samples,
        "failed_flights": len(outcome.failed_flights),
        "failed_flight_ids": outcome.failed_flights[:50],
        "exclusions": dict(outcome.exclusions),
        "phase_grouping": {
            "flights_with_segments": outcome.grouping_flights_with_segments,
            "flights_without_segments": outcome.grouping_flights_without_segments,
            "ungrouped_samples": ungrouped_samples,
        },
    }
    record.metrics = metrics
    record.phase_metrics = phase_metrics
    record.worst_samples = worst_samples
    record.artifacts = []
    finished = datetime.now(UTC)
    duration_s = (finished - record.created_at).total_seconds()
    record.timing = {
        "duration_s": round(duration_s, 3),
        "predict_calls": outcome.timing.calls,
        "predict_seconds_total": round(outcome.timing.seconds, 6),
        "predict_ms_mean": (
            round(outcome.timing.seconds * 1000 / outcome.timing.calls, 3)
            if outcome.timing.calls
            else None
        ),
    }
    if outcome.failed_flights and outcome.samples:
        record.status = "partial"
    elif outcome.failed_flights and not outcome.samples:
        record.status = "failed"
        record.message = "所有航班均失败或没有有效样本。"
    else:
        record.status = "succeeded"
    record.finished_at = finished


def build_report_payload(record: ExperimentRecord) -> dict[str, Any]:
    """实验报告的对外载荷（有界；产物文件与对话工具共用同一结构）。"""
    return {
        "experiment_id": record.experiment_id,
        "kind": record.kind,
        "protocol_version": record.protocol_version,
        "predictor": {
            "id": record.predictor_id,
            "version": record.predictor_version,
            "model_version": record.model_version,
        },
        "versions": {
            "data": record.data_version,
            "feature": record.feature_version,
            "code": record.code_version,
        },
        "hyperparameters": record.hyperparameters,
        "applicability": record.applicability,
        "config": record.config,
        "dataset": record.dataset,
        "coverage": record.coverage,
        "metrics": [item.model_dump(mode="json") for item in record.metrics],
        "phase_metrics": [item.model_dump(mode="json") for item in record.phase_metrics],
        "worst_samples": [item.model_dump(mode="json") for item in record.worst_samples],
        "timing": record.timing,
        "status": record.status,
        "source": record.source,
        "requested_by": record.requested_by,
        "created_at": record.created_at.isoformat(),
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


def export_experiment_report(
    record: ExperimentRecord,
    *,
    store: ExperimentArtifactStore | None = None,
    storage_experiment_id: str | None = None,
) -> dict[str, Any]:
    """把实验报告写入产物文件并返回登记项；写入失败不抛出，返回 status=failed 的登记。"""
    active_store = store if store is not None else ExperimentArtifactStore()
    try:
        metadata = active_store.store(
            experiment_id=storage_experiment_id or record.experiment_id,
            name=REPORT_ARTIFACT_NAME,
            payload=build_report_payload(record),
            kind="report",
        )
    except (OSError, ValueError) as error:
        return {
            "name": REPORT_ARTIFACT_NAME,
            "kind": "report",
            "status": "failed",
            "note": str(error)[:200],
        }
    return {**metadata, "status": "stored"}


def _register_report_artifact(
    record: ExperimentRecord,
    store: ExperimentArtifactStore | None,
) -> None:
    storage_experiment_id = record.experiment_id
    if record.lease_token:
        token_digest = sha256(record.lease_token.encode("utf-8")).hexdigest()[:12]
        storage_experiment_id = (
            f"{record.experiment_id}.attempt-{record.attempt_count}.{token_digest}"
        )
    entry = export_experiment_report(
        record,
        store=store,
        storage_experiment_id=storage_experiment_id,
    )
    record.artifacts = [
        item for item in record.artifacts if item.get("name") != REPORT_ARTIFACT_NAME
    ]
    record.artifacts.append(entry)


async def run_predictor_evaluation(
    *,
    config: EvaluationConfig,
    flight_repository: ScatFlightRepository,
    experiment_repository: ExperimentRepository,
    registry: PredictorRegistry | None = None,
    limit: int | None = None,
    page_size: int = 200,
    progress_interval: int = 20,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    requested_by: str | None = None,
    source: str = "cli",
    phase_lookup: PhaseLookup | None = None,
    phase_grouping: bool = True,
    artifact_store: ExperimentArtifactStore | None = None,
) -> ExperimentRecord:
    """立即运行一次预测器评估实验并写入实验台账与结果。"""
    active_registry = registry if registry is not None else default_registry
    predictor = active_registry.get(config.predictor_id)
    record = _build_record(
        config=config,
        limit=limit,
        predictor=predictor,
        status="running",
        source=source,
        requested_by=requested_by,
        created_at=datetime.now(UTC),
    )
    await experiment_repository.upsert(record)
    task_started_at = perf_counter()
    log_event(
        logger,
        logging.INFO,
        "experiment_task_started",
        task_id=record.experiment_id,
        predictor_id=record.predictor_id,
        batch=record.batch,
        attempt=record.attempt_count,
        source=source,
    )

    async def report_progress(progress: dict[str, Any]) -> None:
        duration_s = max(perf_counter() - task_started_at, 0.001)
        processed = int(progress.get("processed", 0))
        log_event(
            logger,
            logging.INFO,
            "experiment_task_progress",
            task_id=record.experiment_id,
            predictor_id=record.predictor_id,
            batch=record.batch,
            processed_count=processed,
            sample_count=int(progress.get("samples", 0)),
            failed_count=int(progress.get("failed", 0)),
            throughput_per_second=round(processed / duration_s, 3),
            duration_s=round(duration_s, 3),
        )
        if on_progress is not None:
            await on_progress(progress)

    try:
        outcome = await _run_evaluation(
            config=config,
            predictor=predictor,
            limit=limit,
            flight_repository=flight_repository,
            page_size=page_size,
            progress_interval=progress_interval,
            on_progress=report_progress,
            phase_lookup=phase_lookup,
            phase_grouping=phase_grouping,
        )
    except Exception as error:
        log_alert(
            logger,
            "experiment_task_failure",
            task_id=record.experiment_id,
            predictor_id=record.predictor_id,
            batch=record.batch,
            attempt=record.attempt_count,
            failure_type=type(error).__name__,
        )
        raise
    _apply_outcome(record, outcome, config=config, limit=limit)
    _register_report_artifact(record, artifact_store)
    await experiment_repository.upsert(record)
    log_event(
        logger,
        logging.WARNING
        if record.status == "partial"
        else logging.ERROR
        if record.status == "failed"
        else logging.INFO,
        "experiment_task_completed",
        task_id=record.experiment_id,
        predictor_id=record.predictor_id,
        batch=record.batch,
        attempt=record.attempt_count,
        status=record.status,
        processed_count=len(outcome.processed_flights),
        failed_count=len(outcome.failed_flights),
        throughput_per_second=round(
            len(outcome.processed_flights) / max(perf_counter() - task_started_at, 0.001),
            3,
        ),
        duration_s=round(perf_counter() - task_started_at, 3),
    )
    if record.status in {"failed", "partial"}:
        log_alert(
            logger,
            "experiment_task_failure",
            task_id=record.experiment_id,
            predictor_id=record.predictor_id,
            batch=record.batch,
            attempt=record.attempt_count,
            status=record.status,
            failed_count=len(outcome.failed_flights),
        )
    return record


async def run_pending_experiment(
    *,
    experiment_repository: ExperimentRepository,
    flight_repository: ScatFlightRepository,
    registry: PredictorRegistry | None = None,
    experiment_id: str | None = None,
    page_size: int = 200,
    progress_interval: int = 20,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    phase_lookup: PhaseLookup | None = None,
    phase_grouping: bool = True,
    artifact_store: ExperimentArtifactStore | None = None,
    lease_owner: str | None = None,
    lease_ttl_seconds: float | None = None,
    heartbeat_interval_seconds: float | None = None,
) -> ExperimentRecord | None:
    """领取并执行 pending 实验；执行期间用当前 lease token 周期续租。"""
    ttl_seconds = (
        float(lease_ttl_seconds)
        if lease_ttl_seconds is not None
        else float(settings.SCAT_EXPERIMENT_LEASE_SECONDS)
    )
    heartbeat_seconds = (
        float(heartbeat_interval_seconds)
        if heartbeat_interval_seconds is not None
        else float(settings.SCAT_EXPERIMENT_HEARTBEAT_SECONDS)
    )
    if ttl_seconds <= 0:
        raise ValueError("lease_ttl_seconds 必须大于 0")
    if heartbeat_seconds <= 0 or heartbeat_seconds >= ttl_seconds:
        raise ValueError("heartbeat_interval_seconds 必须大于 0 且小于租约时长")
    active_owner = lease_owner or _default_lease_owner()
    lease_ttl = timedelta(seconds=ttl_seconds)
    max_attempts = int(settings.SCAT_EXPERIMENT_MAX_ATTEMPTS)
    document = (
        await experiment_repository.claim(
            experiment_id,
            lease_owner=active_owner,
            lease_ttl=lease_ttl,
            max_attempts=max_attempts,
        )
        if experiment_id is not None
        else await experiment_repository.claim_next(
            lease_owner=active_owner,
            lease_ttl=lease_ttl,
            max_attempts=max_attempts,
        )
    )
    if document is None:
        return None
    payload = {key: value for key, value in document.items() if key != "_id"}
    record = ExperimentRecord.model_validate(payload)
    config = _config_from_record(record)
    limit = record.config.get("limit") if isinstance(record.config.get("limit"), int) else None
    active_registry = registry if registry is not None else default_registry
    predictor = active_registry.get(record.predictor_id)
    lease_token = record.lease_token
    if not lease_token:
        raise ExperimentLeaseLostError(f"实验 {record.experiment_id} 领取后缺少 lease token")
    task_started_at = perf_counter()
    log_event(
        logger,
        logging.INFO,
        "experiment_task_started",
        task_id=record.experiment_id,
        predictor_id=record.predictor_id,
        batch=record.batch,
        attempt=record.attempt_count,
        lease_owner=active_owner,
        source=record.source,
    )

    stop_heartbeat = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _maintain_lease(
            experiment_repository=experiment_repository,
            experiment_id=record.experiment_id,
            lease_token=lease_token,
            lease_ttl=lease_ttl,
            heartbeat_interval_seconds=heartbeat_seconds,
            stop=stop_heartbeat,
            lost=lease_lost,
        ),
        name=f"experiment-heartbeat:{record.experiment_id}",
    )

    async def report_progress(progress: dict[str, Any]) -> None:
        if lease_lost.is_set():
            raise ExperimentLeaseLostError(f"实验 {record.experiment_id} 的租约已失效")
        if on_progress is not None:
            await on_progress(progress)
        duration_s = max(perf_counter() - task_started_at, 0.001)
        processed = int(progress.get("processed", 0))
        log_event(
            logger,
            logging.INFO,
            "experiment_task_progress",
            task_id=record.experiment_id,
            predictor_id=record.predictor_id,
            batch=record.batch,
            attempt=record.attempt_count,
            processed_count=processed,
            sample_count=int(progress.get("samples", 0)),
            failed_count=int(progress.get("failed", 0)),
            throughput_per_second=round(processed / duration_s, 3),
            duration_s=round(duration_s, 3),
        )

    try:
        outcome = await _run_evaluation(
            config=config,
            predictor=predictor,
            limit=limit,
            flight_repository=flight_repository,
            page_size=page_size,
            progress_interval=progress_interval,
            on_progress=report_progress,
            phase_lookup=phase_lookup,
            phase_grouping=phase_grouping,
        )
        if lease_lost.is_set():
            raise ExperimentLeaseLostError(f"实验 {record.experiment_id} 的租约已失效")
        _apply_outcome(record, outcome, config=config, limit=limit)
        _register_report_artifact(record, artifact_store)
        if lease_lost.is_set():
            raise ExperimentLeaseLostError(f"实验 {record.experiment_id} 的租约已失效")
        finished = await experiment_repository.finish(record, lease_token=lease_token)
        if not finished:
            raise ExperimentLeaseLostError(
                f"实验 {record.experiment_id} 的终态提交被 fencing 条件拒绝"
            )
        log_event(
            logger,
            logging.WARNING
            if record.status == "partial"
            else logging.ERROR
            if record.status == "failed"
            else logging.INFO,
            "experiment_task_completed",
            task_id=record.experiment_id,
            predictor_id=record.predictor_id,
            batch=record.batch,
            attempt=record.attempt_count,
            status=record.status,
            processed_count=len(outcome.processed_flights),
            failed_count=len(outcome.failed_flights),
            throughput_per_second=round(
                len(outcome.processed_flights) / max(perf_counter() - task_started_at, 0.001),
                3,
            ),
            duration_s=round(perf_counter() - task_started_at, 3),
        )
        if record.status in {"failed", "partial"}:
            log_alert(
                logger,
                "experiment_task_failure",
                task_id=record.experiment_id,
                predictor_id=record.predictor_id,
                batch=record.batch,
                attempt=record.attempt_count,
                status=record.status,
                failed_count=len(outcome.failed_flights),
            )
        return record
    except Exception as error:
        log_alert(
            logger,
            "experiment_task_failure",
            task_id=record.experiment_id,
            predictor_id=record.predictor_id,
            batch=record.batch,
            attempt=record.attempt_count,
            failure_type=type(error).__name__,
        )
        raise
    finally:
        stop_heartbeat.set()
        await heartbeat_task
