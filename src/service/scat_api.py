"""Deterministic, versioned REST endpoints for SCAT flight data."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from scat.schemas.api import (
    AirportStats,
    BatchOverview,
    BatchPage,
    FlightOverview,
    FlightPage,
    FlightPhaseStats,
    FplOverview,
    PredictionErrorStats,
    PredictionSnapshot,
    PredictionSnapshotPage,
    TrajectoryPage,
)
from scat.services.flight_service import ScatFlightService
from scat.services.materialization_service import MaterializationService
from service.errors import ErrorResponse

BATCH_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"
EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
TRACK_FIELD_GROUPS = {"position", "motion", "mode_s", "quality"}

COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Bearer token is missing or invalid.",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The requested batch, flight, or prediction was not found.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "A path or query parameter is invalid.",
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": ErrorResponse,
        "description": "An unexpected server error occurred.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "A required data dependency is unavailable.",
    },
}

router = APIRouter(
    prefix="/api/v1/scat",
    tags=["SCAT"],
    responses=COMMON_ERROR_RESPONSES,
)

_service = ScatFlightService()
# M4-09：总览统计优先读有效物化结果；读取服务复用同一个 ScatFlightService 实例，
# 实时回退路径与原有聚合口径、缓存完全一致。
_service.materialization_service = MaterializationService(flight_service=_service)


def get_scat_service() -> ScatFlightService:
    """FastAPI dependency hook that can be overridden by integration tests."""
    return _service


ScatService = Annotated[ScatFlightService, Depends(get_scat_service)]
BatchPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=200,
        pattern=BATCH_PATTERN,
        description="SCAT batch identifier, for example scat20161015_20161021.",
    ),
]
ExternalIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=100,
        pattern=EXTERNAL_ID_PATTERN,
        description="Source external flight identifier, not the MongoDB _id.",
    ),
]


def _data_or_404(result: dict[str, Any]) -> Any:
    if result.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("message") or "SCAT resource not found",
        )
    return result["data"]


def _timezone_aware_text(value: datetime | None, field_name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must include a timezone offset",
        )
    return value.isoformat()


def _track_fields(value: str) -> set[str]:
    selected = {item.strip() for item in value.split(",") if item.strip()}
    unknown = selected - TRACK_FIELD_GROUPS
    if not selected or unknown:
        supported = ", ".join(sorted(TRACK_FIELD_GROUPS))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"fields must contain one or more of: {supported}",
        )
    return selected


@router.get(
    "/batches",
    response_model=BatchPage,
    summary="List SCAT data batches",
)
async def list_batches(
    service: ScatService,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    result = await service.list_batches(limit=limit, skip=offset)
    data = result["data"]
    return {
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": data["count"],
            "total": data["total"],
            "has_more": data["has_more"],
        },
        "items": data["items"],
    }


@router.get(
    "/batches/{batch}/overview",
    response_model=BatchOverview,
    summary="Get aggregate statistics for one SCAT batch",
)
async def get_batch_overview(
    batch: BatchPath,
    service: ScatService,
    distribution_limit: Annotated[int, Query(ge=1, le=50)] = 10,
    time_granularity: Literal["day", "hour"] = "day",
    statistics_source: Literal["auto", "live"] = "auto",
) -> dict[str, Any]:
    result = await service.get_batch_overview(
        batch=batch,
        distribution_limit=distribution_limit,
        time_granularity=time_granularity,
        statistics_source=statistics_source,
    )
    return _data_or_404(result)


@router.get(
    "/flights",
    response_model=FlightPage,
    summary="Search SCAT flights using structured filters",
)
async def search_flights(
    service: ScatService,
    batch: Annotated[str | None, Query(min_length=1, max_length=200, pattern=BATCH_PATTERN)] = None,
    callsign: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    aircraft_type: Annotated[str | None, Query(min_length=2, max_length=10)] = None,
    adep: Annotated[str | None, Query(min_length=3, max_length=4)] = None,
    ades: Annotated[str | None, Query(min_length=3, max_length=4)] = None,
    candidate_key: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
    start_time: Annotated[
        datetime | None,
        Query(description="Inclusive UTC IOBT lower bound."),
    ] = None,
    end_time: Annotated[
        datetime | None,
        Query(description="Exclusive UTC IOBT upper bound."),
    ] = None,
    sort_by: Literal["iobt", "callsign", "external_id"] = "iobt",
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    result = await service.search_flights(
        batch=batch,
        callsign=callsign,
        aircraft_type=aircraft_type,
        adep=adep,
        ades=ades,
        candidate_key=candidate_key,
        start_time=start_time,
        end_time=end_time,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        skip=offset,
    )
    data = result["data"]
    return {
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": data["count"],
            "total": data["total"],
            "has_more": data["has_more"],
        },
        "items": data["items"],
    }


@router.get(
    "/flights/{batch}/{external_id}",
    response_model=FlightOverview,
    summary="Get one flight and its analysis summaries",
)
async def get_flight(
    batch: BatchPath,
    external_id: ExternalIdPath,
    service: ScatService,
) -> dict[str, Any]:
    result = await service.get_flight_overview(batch=batch, external_id=external_id)
    return _data_or_404(result)


@router.get(
    "/flights/{batch}/{external_id}/trajectory",
    response_model=TrajectoryPage,
    response_model_exclude_none=True,
    summary="Get a bounded page of normalized trajectory points",
)
async def get_trajectory(
    batch: BatchPath,
    external_id: ExternalIdPath,
    service: ScatService,
    start: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    start_time: Annotated[
        datetime | None,
        Query(description="Inclusive UTC ISO 8601 track-time lower bound."),
    ] = None,
    end_time: Annotated[
        datetime | None,
        Query(description="Exclusive UTC ISO 8601 track-time upper bound."),
    ] = None,
    every_nth: Annotated[
        int,
        Query(ge=1, le=20, description="Return one point for every N source points."),
    ] = 1,
    fields: Annotated[
        str,
        Query(description="Comma-separated groups: position,motion,mode_s,quality."),
    ] = "position,motion,mode_s,quality",
) -> dict[str, Any]:
    normalized_start_time = _timezone_aware_text(start_time, "start_time")
    normalized_end_time = _timezone_aware_text(end_time, "end_time")
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time must be earlier than end_time",
        )
    result = await service.get_track_points(
        batch=batch,
        external_id=external_id,
        start=start,
        limit=limit,
        start_time=normalized_start_time,
        end_time=normalized_end_time,
        every_nth=every_nth,
        fields=_track_fields(fields),
    )
    return _data_or_404(result)


@router.get(
    "/flights/{batch}/{external_id}/fpl",
    response_model=FplOverview,
    summary="Get the normalized flight-plan event summary",
)
async def get_fpl(
    batch: BatchPath,
    external_id: ExternalIdPath,
    service: ScatService,
) -> dict[str, Any]:
    result = await service.get_fpl_overview(batch=batch, external_id=external_id)
    return _data_or_404(result)


@router.get(
    "/flights/{batch}/{external_id}/predictions",
    response_model=PredictionSnapshotPage,
    summary="List SCAT prediction snapshots without full route arrays",
)
async def list_predictions(
    batch: BatchPath,
    external_id: ExternalIdPath,
    service: ScatService,
    start: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    result = await service.list_prediction_snapshots(
        batch=batch,
        external_id=external_id,
        start=start,
        limit=limit,
    )
    return _data_or_404(result)


@router.get(
    "/flights/{batch}/{external_id}/prediction-snapshots/{snapshot_time}",
    response_model=PredictionSnapshot,
    summary="Get the latest SCAT prediction at or before a UTC time",
)
async def get_prediction_snapshot(
    batch: BatchPath,
    external_id: ExternalIdPath,
    snapshot_time: Annotated[
        datetime,
        Path(description="Timezone-aware ISO 8601 lookup time."),
    ],
    service: ScatService,
) -> dict[str, Any]:
    if snapshot_time.tzinfo is None or snapshot_time.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="snapshot_time must include a timezone offset",
        )

    result = await service.get_prediction_snapshot(
        batch=batch,
        external_id=external_id,
        timestamp=snapshot_time.isoformat(),
    )
    return _data_or_404(result)


@router.get(
    "/batches/{batch}/airports",
    response_model=AirportStats,
    summary="Get departure airport flight counts and delay statistics",
)
async def get_airport_stats(
    batch: BatchPath,
    service: ScatService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Aggregate per-airport flight counts and ATD-IOBT departure delays."""
    result = await service.get_airport_stats(batch=batch, limit=limit)
    return _data_or_404(result)


@router.get(
    "/flights/{batch}/{external_id}/phases",
    response_model=FlightPhaseStats,
    summary="Get climb/cruise/descent phase statistics for one flight",
)
async def get_flight_phases(
    batch: BatchPath,
    external_id: ExternalIdPath,
    service: ScatService,
) -> dict[str, Any]:
    """Group track points by vertical mode and aggregate per-phase statistics."""
    result = await service.get_flight_phase_stats(
        batch=batch,
        external_id=external_id,
    )
    return _data_or_404(result)


@router.get(
    "/flights/{batch}/{external_id}/prediction-error",
    response_model=PredictionErrorStats,
    summary="Compare the latest SCAT prediction against the actual track",
)
async def get_prediction_error_stats(
    batch: BatchPath,
    external_id: ExternalIdPath,
    service: ScatService,
    timestamp: Annotated[
        str | None,
        Query(description="Optional UTC ISO 8601 lookup time; defaults to the latest snapshot."),
    ] = None,
    match_window_seconds: Annotated[
        float,
        Query(ge=10, le=3600, description="Max ETO-to-track matching window in seconds."),
    ] = 300.0,
) -> dict[str, Any]:
    """Compute horizontal, vertical, and time errors between prediction and track."""
    result = await service.get_prediction_error_stats(
        batch=batch,
        external_id=external_id,
        timestamp=timestamp,
        match_window_seconds=match_window_seconds,
    )
    return _data_or_404(result)
