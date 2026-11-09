"""M4-09 read endpoints: materialized batch statistics and materialization jobs."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from scat.schemas.api import (
    MaterializationJobPage,
    MaterializationJobSummary,
    MaterializedBatchStatistics,
)
from scat.services.materialization_service import (
    JOB_LIST_LIMIT_DEFAULT,
    JOB_LIST_LIMIT_MAX,
    MaterializationService,
)
from service.scat_api import BATCH_PATTERN, COMMON_ERROR_RESPONSES

router = APIRouter(
    prefix="/api/v1/scat/materialization",
    tags=["SCAT Materialization"],
    responses=COMMON_ERROR_RESPONSES,
)

_service = MaterializationService()


def get_materialization_service() -> MaterializationService:
    """FastAPI dependency hook that integration tests can override."""
    return _service


MaterializationReader = Annotated[MaterializationService, Depends(get_materialization_service)]
BatchPath = Annotated[str, Path(min_length=1, max_length=200, pattern=BATCH_PATTERN)]
JobIdPath = Annotated[
    str,
    Path(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$"),
]
JobKind = Literal[
    "batch_statistics",
    "route_statistics",
    "flight_features",
    "phase_segments",
    "prediction_metrics",
]
JobStatus = Literal["pending", "running", "succeeded", "partial", "failed"]


def _data_or_404(result: dict[str, Any]) -> Any:
    if result.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("message") or "Materialization resource not found",
        )
    return result["data"]


@router.get(
    "/batches/{batch}/statistics",
    response_model=MaterializedBatchStatistics,
    summary="Read batch statistics from materialized results, falling back to live aggregation",
)
async def get_materialized_batch_statistics(
    batch: BatchPath,
    service: MaterializationReader,
    statistics_source: Literal["auto", "live"] = Query(
        default="auto",
        description="auto: 有效物化结果优先，缺失/过期时实时计算并说明原因。",
    ),
) -> Any:
    result = await service.read_batch_statistics(
        batch=batch,
        statistics_source=statistics_source,
    )
    return _data_or_404(result)


@router.get(
    "/batches/{batch}/metric-summaries",
    summary="Read materialized prediction metric summaries (M5-08 evaluation page data)",
)
async def get_metric_summaries(
    batch: BatchPath,
    service: MaterializationReader,
    group_by: Annotated[str | None, Query(max_length=32)] = None,
    horizon_s: Annotated[float | None, Query(gt=0, le=3600)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Any:
    result = await service.list_metric_summaries(
        batch=batch,
        group_by=group_by,
        horizon_s=horizon_s,
        limit=limit,
    )
    return _data_or_404({"status": "ok", "data": result})


@router.get(
    "/batches/{batch}/worst-prediction-samples",
    summary="Read worst prediction samples with phase and aircraft type (M5-07/M5-08)",
)
async def get_worst_prediction_samples(
    batch: BatchPath,
    service: MaterializationReader,
    metric: Annotated[str, Query(pattern="^(horizontal|vertical|along|cross)$")] = "horizontal",
    horizon_s: Annotated[float | None, Query(gt=0, le=3600)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> Any:
    field = {
        "horizontal": "horizontal_error_m",
        "vertical": "vertical_error_m",
        "along": "along_track_error_m",
        "cross": "cross_track_error_m",
    }[metric]
    result = await service.list_worst_prediction_samples(
        batch=batch,
        metric=field,
        horizon_s=horizon_s,
        limit=limit,
    )
    return _data_or_404({"status": "ok", "data": result})


@router.get(
    "/jobs",
    response_model=MaterializationJobPage,
    summary="List materialization jobs with progress, failures and retry lineage",
)
async def list_materialization_jobs(
    service: MaterializationReader,
    kind: JobKind | None = None,
    batch: Annotated[str | None, Query(min_length=1, max_length=200, pattern=BATCH_PATTERN)] = None,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=JOB_LIST_LIMIT_MAX)] = JOB_LIST_LIMIT_DEFAULT,
) -> Any:
    result = await service.list_jobs(
        kind=kind,
        batch=batch,
        status_filter=status_filter,
        limit=limit,
    )
    return _data_or_404(result)


@router.get(
    "/jobs/{job_id}",
    response_model=MaterializationJobSummary,
    summary="Read one materialization job, including failed units and reasons",
)
async def get_materialization_job(job_id: JobIdPath, service: MaterializationReader) -> Any:
    result = await service.get_job(job_id)
    return _data_or_404(result)
