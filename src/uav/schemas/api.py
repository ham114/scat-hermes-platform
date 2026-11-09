"""Versioned response contracts for the deterministic UAV REST API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class UavApiModel(BaseModel):
    """Base model that rejects accidental response-contract expansion."""

    model_config = ConfigDict(extra="forbid")


class UavPageInfo(UavApiModel):
    offset: int = Field(description="Zero-based offset of this page.")
    limit: int = Field(description="Maximum number of records requested.")
    returned: int = Field(description="Number of records returned in this page.")
    total: int = Field(description="Total records matching the query.")
    has_more: bool = Field(description="Whether another page is available.")


class UavModelSummary(UavApiModel):
    model: str = Field(description="UAV aircraft model, for example 0001 or 0007.")
    flight_count: int = Field(description="Number of flights recorded for this model.")
    earliest_takeoff_utc: UtcDateTime | None = Field(
        default=None, description="Earliest takeoff time, UTC ISO 8601."
    )
    latest_takeoff_utc: UtcDateTime | None = Field(
        default=None, description="Latest takeoff time, UTC ISO 8601."
    )


class UavModelPage(UavApiModel):
    pagination: UavPageInfo
    items: list[UavModelSummary]


class UavFlightSummary(UavApiModel):
    external_id: str = Field(description="ASCII sanitized source file identifier.")
    model: str = Field(description="Aircraft model, for example 0001.")
    tail_number: str | None = Field(
        default=None, description="Tail number for model 0007 (1-4); null otherwise."
    )
    mission: str | None = Field(default=None, description="Mission tag DTC or MDC.")
    flight_seq: int | None = Field(
        default=None, description="Flight sequence number (第N飞), when available."
    )
    takeoff_at: UtcDateTime | None = Field(
        default=None, description="Takeoff time in UTC, derived from the file name."
    )
    duration_s: float | None = Field(default=None, description="Flight duration in seconds.")
    sample_count: int = Field(description="Number of sensor samples.")
    sample_rate_hz: float | None = Field(default=None, description="Sample rate in Hz.")
    flight_kind: str = Field(description="normal or anomaly_sample.")
    label: str | None = Field(default=None, description="Anomaly label, when applicable.")


class UavFlightPage(UavApiModel):
    pagination: UavPageInfo
    items: list[UavFlightSummary]


class UavRangeStat(UavApiModel):
    min: float | None = None
    max: float | None = None


class UavPositionBounds(UavApiModel):
    min_lat: float | None = None
    max_lat: float | None = None
    min_lon: float | None = None
    max_lon: float | None = None


class UavChannelStat(UavApiModel):
    key: str = Field(description="ASCII channel key, for example cmp_comb_alt.")
    min: float | None = None
    max: float | None = None
    avg: float | None = None
    missing: int = Field(description="Number of samples where the channel was absent.")


class UavFlightSummaryStats(UavApiModel):
    altitude_m: UavRangeStat
    ground_speed_mps: UavRangeStat
    position_bounds: UavPositionBounds


class UavFlightDetail(UavApiModel):
    external_id: str
    model: str
    tail_number: str | None = None
    mission: str | None = None
    flight_seq: int | None = None
    takeoff_at: UtcDateTime | None = None
    duration_s: float | None = None
    sample_count: int
    sample_rate_hz: float | None = None
    flight_kind: str
    label: str | None = None
    platform: str | None = None
    original_filename: str | None = Field(
        default=None, description="ASCII sanitized original file name."
    )
    uploaded_at: UtcDateTime | None = Field(default=None)
    header_layout_note: str | None = Field(
        default=None,
        description="Non-null when the source file header had to be handled specially.",
    )
    extra_column_count: int = Field(
        default=0, description="Columns beyond the standard 67 that were ignored."
    )
    summary: UavFlightSummaryStats
    channel_stats: list[UavChannelStat] = Field(
        default_factory=list, description="Per-channel min/max/avg/missing statistics."
    )


class UavChannelInfo(UavApiModel):
    key: str
    label: str
    unit: str
    group: str


class UavSamplePage(UavApiModel):
    external_id: str
    start_time: UtcDateTime | None = Field(
        default=None,
        description="UTC timestamp represented by time_step_s=0 in a chart.",
    )
    start: int = Field(description="Sequence index of the first returned sample.")
    limit: int
    every_nth: int = Field(description="Downsampling step applied to the window.")
    returned: int = Field(description="Number of samples returned.")
    has_more: bool
    next_start: int | None = Field(
        default=None, description="Sequence index for the next page; null when exhausted."
    )
    samples: list[dict[str, Any]] = Field(
        description=(
            "Sensor samples. Each entry contains seq, t_s and the requested "
            "cmp_* channels; missing channel values are omitted."
        )
    )


class UavChannelStatRow(UavApiModel):
    channel: str
    label: str | None = None
    unit: str | None = None
    group: str | None = None
    flight_count: int
    total_samples: int
    weighted_mean: float | None = None
    min: float | None = None
    max: float | None = None
    missing_total: int


class UavChannelStats(UavApiModel):
    flights_included: int
    items: list[UavChannelStatRow]
    note: str | None = None


class UavKindStat(UavApiModel):
    flight_count: int
    total_samples: int
    weighted_mean: float | None = None
    min: float | None = None
    max: float | None = None
    missing_total: int


class UavKindComparisonRow(UavApiModel):
    channel: str
    label: str | None = None
    unit: str | None = None
    group: str | None = None
    normal: UavKindStat | None = None
    anomaly: UavKindStat | None = None
    delta_mean: float | None = None
    delta_relative: float | None = None


class UavNormalVsAnomalyStats(UavApiModel):
    items: list[UavKindComparisonRow]
    note: str | None = None


class UavPhaseRow(UavApiModel):
    phase: str = Field(description="climb, level, or descent.")
    point_count: int
    point_ratio: float | None = None
    start_t_s: float | None = None
    end_t_s: float | None = None
    duration_seconds: float | None = None
    min_altitude_m: float | None = None
    max_altitude_m: float | None = None
    average_altitude_m: float | None = None
    average_ground_speed_mps: float | None = None
    maximum_ground_speed_mps: float | None = None
    average_vertical_speed_mps: float | None = None


class UavFlightPhaseStats(UavApiModel):
    total_points: int
    phases: list[UavPhaseRow]
    phase_threshold_mps: float
