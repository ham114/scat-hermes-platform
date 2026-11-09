"""Deterministic, versioned REST endpoints for UAV internal sensor data."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from service.errors import ErrorResponse
from uav.schemas.api import (
    UavChannelInfo,
    UavChannelStats,
    UavFlightDetail,
    UavFlightPage,
    UavFlightPhaseStats,
    UavModelPage,
    UavNormalVsAnomalyStats,
    UavSamplePage,
)
from uav.schemas.channels import CHANNEL_GROUPS
from uav.services.uav_service import (
    EVERY_NTH_MAX,
    SAMPLE_LIMIT_MAX,
    UavFlightService,
)

EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
SORT_BY_VALUES = {"takeoff_at", "external_id", "duration_s", "sample_count"}

COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Bearer token is missing or invalid.",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The requested UAV flight was not found.",
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
    prefix="/api/v1/uav",
    tags=["UAV"],
    responses=COMMON_ERROR_RESPONSES,
)

_service = UavFlightService()


def get_uav_service() -> UavFlightService:
    """FastAPI dependency hook that can be overridden by integration tests."""
    return _service


UavService = Annotated[UavFlightService, Depends(get_uav_service)]

ExternalIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=100,
        pattern=EXTERNAL_ID_PATTERN,
        description="ASCII sanitized source file identifier, for example 20230330122740_DTC_53.1394B.",
    ),
]


def _data_or_404(result: dict[str, Any]) -> Any:
    if result.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("message") or "UAV resource not found",
        )
    return result["data"]


def _utc_aware(value: str | None, field_name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be an ISO 8601 timestamp with timezone offset",
        )
    return parsed


@router.get(
    "/models",
    response_model=UavModelPage,
    summary="List UAV aircraft models",
)
async def list_models(service: UavService) -> Any:
    """Aggregate flight counts and takeoff ranges per aircraft model."""
    return _data_or_404(await service.list_models())


@router.get(
    "/flights",
    response_model=UavFlightPage,
    summary="Search UAV flights",
)
async def search_uav_flights(
    service: UavService,
    model: Annotated[
        str | None,
        Query(description="Aircraft model, for example 0001 or 0007."),
    ] = None,
    tail_number: Annotated[
        str | None,
        Query(description="Tail number for model 0007, for example 1."),
    ] = None,
    mission: Annotated[
        str | None,
        Query(description="Mission tag, DTC or MDC."),
    ] = None,
    flight_kind: Annotated[
        str | None,
        Query(description="normal or anomaly_sample."),
    ] = None,
    label: Annotated[
        str | None,
        Query(description="Anomaly label, for example height_anomaly."),
    ] = None,
    takeoff_start: Annotated[
        str | None,
        Query(description="Inclusive lower bound on takeoff time, UTC ISO 8601."),
    ] = None,
    takeoff_end: Annotated[
        str | None,
        Query(description="Exclusive upper bound on takeoff time, UTC ISO 8601."),
    ] = None,
    sort_by: Annotated[
        str,
        Query(description="Sort field."),
    ] = "takeoff_at",
    sort_order: Annotated[
        int,
        Query(ge=-1, le=1, description="1 ascending, -1 descending."),
    ] = 1,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Page size."),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Zero-based page offset."),
    ] = 0,
) -> Any:
    """List UAV flights with structured filters and stable pagination."""
    if sort_by not in SORT_BY_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"sort_by must be one of: {', '.join(sorted(SORT_BY_VALUES))}",
        )
    result = await service.list_flights(
        model=model,
        tail_number=tail_number,
        mission=mission,
        flight_kind=flight_kind,
        label=label,
        start_time=_utc_aware(takeoff_start, "takeoff_start"),
        end_time=_utc_aware(takeoff_end, "takeoff_end"),
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    return _data_or_404(result)


@router.get(
    "/flights/{external_id}",
    response_model=UavFlightDetail,
    summary="Get UAV flight detail",
)
async def get_uav_flight(
    service: UavService,
    external_id: ExternalIdPath,
) -> Any:
    """Return identity, ingest metadata and channel statistics (no samples)."""
    return _data_or_404(await service.get_flight(external_id))


@router.get(
    "/flights/{external_id}/samples",
    response_model=UavSamplePage,
    summary="Page through UAV sensor samples",
)
async def get_samples(
    service: UavService,
    external_id: ExternalIdPath,
    start: Annotated[
        int,
        Query(ge=0, description="Sequence index of the first sample."),
    ] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=SAMPLE_LIMIT_MAX, description="Window size before downsampling."),
    ] = 200,
    every_nth: Annotated[
        int,
        Query(ge=1, le=EVERY_NTH_MAX, description="Keep every Nth sample of the window."),
    ] = 1,
    fields: Annotated[
        str | None,
        Query(
            description=(
                "Comma separated channel groups to keep: "
                "position, velocity, attitude, rates, accel, air. "
                "Omit to return all channels."
            ),
        ),
    ] = None,
    channels: Annotated[
        str | None,
        Query(
            description=(
                "Comma separated exact channel keys. Omit to return every channel "
                "in the selected groups."
            ),
        ),
    ] = None,
) -> Any:
    """Return a bounded, optionally downsampled slice of the samples array."""
    selected = None
    if fields:
        parsed = {item.strip() for item in fields.split(",") if item.strip()}
        unknown = parsed - set(CHANNEL_GROUPS)
        if not parsed or unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "fields must contain one or more of: "
                    + ", ".join(sorted(CHANNEL_GROUPS))
                ),
            )
        selected = parsed
    selected_channels = None
    if channels:
        selected_channels = {
            item.strip() for item in channels.split(",") if item.strip()
        }
    try:
        result = await service.get_samples(
            external_id,
            start=start,
            limit=limit,
            every_nth=every_nth,
            fields=selected,
            channels=selected_channels,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return _data_or_404(result)


@router.get(
    "/channels",
    response_model=list[UavChannelInfo],
    summary="List UAV channel catalog",
)
async def list_channels(service: UavService) -> Any:
    """Return the 34-channel catalog (key, label, unit, group) for chart rendering."""
    return _data_or_404(service.list_channels())["items"]


def _parse_channels(value: str | None) -> set[str]:
    """把逗号分隔的通道键解析为集合；缺省返回空集合。"""
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


@router.get(
    "/stats/channels",
    response_model=UavChannelStats,
    summary="Aggregate channel statistics across flights",
)
async def get_channel_stats(
    service: UavService,
    channels: Annotated[
        str,
        Query(
            min_length=1,
            description="Comma separated channel keys, at most 6, for example cmp_comb_alt,cmp_ground_speed.",
        ),
    ],
    model: Annotated[
        str | None,
        Query(description="Optional aircraft model filter, for example 0007."),
    ] = None,
    flight_kind: Annotated[
        str | None,
        Query(description="Optional normal or anomaly_sample filter."),
    ] = None,
) -> Any:
    """Aggregate weighted mean, min, max, and missing counts per channel."""
    if flight_kind is not None and flight_kind not in {"normal", "anomaly_sample"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="flight_kind must be normal or anomaly_sample",
        )
    try:
        result = await service.get_channel_stats(
            channels=_parse_channels(channels),
            model=model,
            flight_kind=flight_kind,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return _data_or_404(result)


@router.get(
    "/stats/normal-vs-anomaly",
    response_model=UavNormalVsAnomalyStats,
    summary="Compare channel statistics between normal and anomaly flights",
)
async def get_normal_vs_anomaly(
    service: UavService,
    channels: Annotated[
        str,
        Query(
            min_length=1,
            description="Comma separated channel keys, at most 6.",
        ),
    ],
    model: Annotated[
        str | None,
        Query(description="Optional aircraft model filter."),
    ] = None,
) -> Any:
    """Return per-channel weighted means and deltas for normal vs anomaly groups."""
    try:
        result = await service.get_normal_vs_anomaly_stats(
            channels=_parse_channels(channels),
            model=model,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return _data_or_404(result)


@router.get(
    "/flights/{external_id}/phases",
    response_model=UavFlightPhaseStats,
    summary="Get climb/level/descent phase statistics for one UAV flight",
)
async def get_uav_flight_phases(
    external_id: ExternalIdPath,
    service: UavService,
) -> Any:
    """Segment samples by combined vertical speed and aggregate per-phase stats."""
    return _data_or_404(await service.get_flight_phase_stats(external_id))
