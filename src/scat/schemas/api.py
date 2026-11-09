"""Versioned response contracts for the deterministic SCAT REST API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class ScatApiModel(BaseModel):
    """Base model that rejects accidental response-contract expansion."""

    model_config = ConfigDict(extra="forbid")


class PageInfo(ScatApiModel):
    offset: int = Field(description="Zero-based offset of this page.")
    limit: int = Field(description="Maximum number of records requested.")
    returned: int = Field(description="Number of records returned in this page.")
    total: int = Field(description="Total records matching the query.")
    has_more: bool = Field(description="Whether another page is available.")


class BatchSummary(ScatApiModel):
    batch: str = Field(description="SCAT import batch identifier.")
    flight_count: int = Field(description="Number of flights in the batch.")
    earliest_iobt_utc: UtcDateTime | None = Field(
        default=None,
        description="Earliest initial off-block time, UTC ISO 8601.",
    )
    latest_iobt_utc: UtcDateTime | None = Field(
        default=None,
        description="Latest initial off-block time, UTC ISO 8601.",
    )
    earliest_upload_time: UtcDateTime | None = Field(default=None)
    latest_upload_time: UtcDateTime | None = Field(default=None)


class BatchPage(ScatApiModel):
    pagination: PageInfo
    items: list[BatchSummary]


class BatchStatistics(ScatApiModel):
    batch: str
    flight_count: int
    unique_callsign_count: int
    aircraft_type_count: int
    departure_airport_count: int
    arrival_airport_count: int
    total_track_points: int
    average_track_points: float
    minimum_track_points: int
    maximum_track_points: int
    flights_with_prediction: int
    total_prediction_snapshots: int


class AircraftTypeCount(ScatApiModel):
    aircraft_type: str
    flight_count: int


class RouteCount(ScatApiModel):
    adep: str = Field(description="ICAO departure airport code.")
    ades: str = Field(description="ICAO destination airport code.")
    flight_count: int


class AirportCount(ScatApiModel):
    airport: str = Field(description="ICAO airport code.")
    flight_count: int


class FlightVolumePoint(ScatApiModel):
    period_start: UtcDateTime = Field(
        description="UTC start of the hourly or daily aggregation period."
    )
    flight_count: int


class BatchDataQuality(ScatApiModel):
    missing_callsign_count: int
    missing_aircraft_type_count: int
    missing_departure_airport_count: int
    missing_arrival_airport_count: int
    missing_iobt_count: int
    empty_trajectory_count: int
    flights_without_prediction_count: int


class BatchOverview(ScatApiModel):
    statistics: BatchStatistics
    top_aircraft_types: list[AircraftTypeCount]
    top_routes: list[RouteCount]
    top_departure_airports: list[AirportCount]
    top_arrival_airports: list[AirportCount]
    flight_volume: list[FlightVolumePoint]
    data_quality: BatchDataQuality
    # M4-09：统计来源。materialized 表示直接读 scat_batch_statistics，未扫描原始集合。
    statistics_source: Literal["materialized", "live"] = "live"
    statistics_generated_at: UtcDateTime | None = None


class MaterializedBatchStatistics(ScatApiModel):
    """M4-09：批次统计的读取结果（有效物化结果优先，否则实时回退并说明原因）。"""

    batch: str
    source: Literal["materialized", "live"]
    statistics: BatchStatistics
    data_quality: BatchDataQuality
    generated_at: UtcDateTime | None = None
    source_version: str | None = None
    algorithm_version: str | None = None
    fallback_reason: Literal["missing", "expired"] | None = Field(
        default=None,
        description="source=live 时说明为何没读到有效物化结果。",
    )


class MaterializationJobSummary(ScatApiModel):
    """M4-09：物化任务状态、进度与失败单位（可据此发起重试）。"""

    job_id: str
    kind: str
    batch: str
    status: Literal["pending", "running", "succeeded", "partial", "failed"]
    progress: float
    total_count: int
    processed_count: int
    failed_count: int
    failed_targets: list[str]
    failure_detail: dict[str, str] = Field(default_factory=dict)
    error_message: str | None = None
    retry_of: str | None = None
    generated_at: UtcDateTime
    expires_at: UtcDateTime | None = None


class MaterializationJobPage(ScatApiModel):
    items: list[MaterializationJobSummary]
    limit: int
    returned: int


class FlightSource(ScatApiModel):
    system: str | None = None
    batch: str | None = None
    external_id: str | None = None
    external_id_type: str | None = None


class FlightIdentity(ScatApiModel):
    callsign: str | None = None
    aircraft_type: str | None = None
    departure_airport: str | None = None
    destination_airport: str | None = None
    iobt_utc: UtcDateTime | None = None
    atd_utc: UtcDateTime | None = None
    candidate_key: str | None = None
    identity_confidence: float | str | None = None


class IngestInfo(ScatApiModel):
    original_filename: str | None = None
    uploaded_at: UtcDateTime | None = None


class FlightSummary(ScatApiModel):
    dataset_id: int | str | None = None
    source: FlightSource
    identity: FlightIdentity
    control_centre_transfer_count: int
    control_centre_history: list[dict[str, Any]]
    ingest: IngestInfo


class FlightPage(ScatApiModel):
    pagination: PageInfo
    items: list[FlightSummary]


class GeographicBounds(ScatApiModel):
    min_lat: float | None = Field(default=None, description="Minimum latitude in degrees.")
    max_lat: float | None = Field(default=None, description="Maximum latitude in degrees.")
    min_lon: float | None = Field(default=None, description="Minimum longitude in degrees.")
    max_lon: float | None = Field(default=None, description="Maximum longitude in degrees.")


class AltitudeSummary(ScatApiModel):
    minimum_flight_level: float | None = None
    maximum_flight_level: float | None = None
    average_flight_level: float | None = None
    minimum_metres: float | None = None
    maximum_metres: float | None = None
    average_metres: float | None = None


class GroundSpeedSummary(ScatApiModel):
    average_mps: float | None = None
    maximum_mps: float | None = None
    average_knots: float | None = None
    maximum_knots: float | None = None


class VerticalRateSummary(ScatApiModel):
    average_fpm: float | None = None
    maximum_fpm: float | None = None
    average_mps: float | None = None
    maximum_mps: float | None = None


class VerticalModes(ScatApiModel):
    level: int | float
    climb: int | float
    descent: int | float
    uncertain: int | float


class TrajectoryQuality(ScatApiModel):
    missing_position_points: int
    missing_motion_points: int
    mode_s_present_points: int
    mode_s_coverage_ratio: float
    unique_timestamp_count: int
    duplicate_timestamp_count: int
    has_duplicate_timestamps: bool
    is_model_input_candidate: bool


class TrajectorySummary(ScatApiModel):
    point_count: int
    start_time: UtcDateTime | None = None
    end_time: UtcDateTime | None = None
    duration_seconds: float | None = None
    duration_minutes: float | None = None
    geographic_bounds: GeographicBounds
    altitude: AltitudeSummary
    ground_speed: GroundSpeedSummary
    absolute_vertical_rate: VerticalRateSummary
    vertical_mode_counts: VerticalModes
    vertical_mode_ratios: VerticalModes
    data_quality: TrajectoryQuality


class FplEventCounts(ScatApiModel):
    base: int = 0
    departure: int = 0
    arrival: int = 0
    clearance: int = 0
    holding: int = 0
    plan_update: int = 0


class DeparturePlan(ScatApiModel):
    runway: str | None = None
    sid: str | None = None
    iobt: UtcDateTime | None = None
    atd: UtcDateTime | None = None
    last_update_time: UtcDateTime | None = None


class ArrivalPlan(ScatApiModel):
    runway: str | None = None
    star: str | None = None
    approach_clearance: bool | str | int | None = None
    missed_approach: bool | str | int | None = None
    last_update_time: UtcDateTime | None = None


class ClearancePlan(ScatApiModel):
    cleared_level: float | str | None = None
    cleared_level_unit: str | None = None
    assigned_speed: float | str | None = None
    assigned_speed_unit: str | None = None
    assigned_heading: float | str | None = None
    assigned_heading_beacon: str | None = None
    last_update_time: UtcDateTime | None = None


class HoldingPlan(ScatApiModel):
    had_holding: bool
    latest_status: str | None = None
    latest_stack_status: str | None = None
    entry_time: UtcDateTime | None = None
    leaving_time: UtcDateTime | None = None


class RoutePlan(ScatApiModel):
    icao_route: str | None = None
    requested_flight_level: str | float | None = None
    true_airspeed: str | float | None = None
    entry_point: str | None = None
    exit_point: str | None = None
    last_update_time: UtcDateTime | None = None


class FplOverview(ScatApiModel):
    event_counts: FplEventCounts
    departure: DeparturePlan
    arrival: ArrivalPlan
    latest_clearance: ClearancePlan
    holding: HoldingPlan
    route_plan: RoutePlan
    had_approach_clearance: bool


class PredictionSummary(ScatApiModel):
    available: bool
    snapshot_count: int
    first_snapshot_time: UtcDateTime | None = None
    last_snapshot_time: UtcDateTime | None = None
    minimum_route_points: int | None = None
    maximum_route_points: int | None = None
    average_route_points: float | None = None
    source: str
    note: str


class FlightOverview(ScatApiModel):
    flight: FlightSummary
    trajectory: TrajectorySummary | None = None
    flight_plan: FplOverview | None = None
    scat_prediction: PredictionSummary | None = None


class TrackPosition(ScatApiModel):
    lat_deg: float | None = None
    lon_deg: float | None = None
    flight_level: float | None = None
    altitude_ft: float | None = None
    altitude_m: float | None = None


class TrackMotion(ScatApiModel):
    vx_mps: float | None = None
    vy_mps: float | None = None
    ground_speed_mps: float | None = None
    ground_speed_knots: float | None = None
    track_angle_deg: float | None = None
    rocd_fpm: float | None = None
    vertical_speed_mps: float | None = None
    vertical_mode: int | str | None = None
    vertical_mode_name: str | None = None


class ModeSData(ScatApiModel):
    available: bool
    barometric_vertical_rate_fpm: float | int | None = None
    indicated_airspeed_knots: float | int | None = None
    mach: float | None = None
    magnetic_heading_deg: float | None = None
    selected_altitude: float | int | None = None
    mode_s_altitude_ft: float | int | None = None


class PointQuality(ScatApiModel):
    valid_for_model_input: bool
    missing_fields: list[str]


class TrackPoint(ScatApiModel):
    sequence: int
    timestamp: UtcDateTime | None = None
    position: TrackPosition | None = None
    motion: TrackMotion | None = None
    mode_s: ModeSData | None = None
    quality: PointQuality | None = None


class TrajectoryPageInfo(ScatApiModel):
    start: int
    returned_points: int
    source_points_consumed: int
    total_points: int
    next_start: int | None = None
    has_more: bool


class TrajectoryPage(ScatApiModel):
    flight: FlightSummary
    pagination: TrajectoryPageInfo
    points: list[TrackPoint]


class PredictionSnapshotItem(ScatApiModel):
    time: UtcDateTime
    route_point_count: int


class PredictionPageInfo(ScatApiModel):
    start: int
    returned_snapshots: int
    total_snapshots: int
    has_more: bool


class PredictionSnapshotPage(ScatApiModel):
    flight: FlightSummary
    pagination: PredictionPageInfo
    snapshots: list[PredictionSnapshotItem]


class PredictionRoutePoint(ScatApiModel):
    sequence: int
    fix_name: str | None = None
    fix_kind: str | None = None
    lat_deg: float | None = None
    lon_deg: float | None = None
    estimated_time_over: UtcDateTime | None = None
    actual_flight_level: float | None = None
    actual_altitude_m: float | None = None
    requested_flight_level: float | str | None = None
    is_actual_time_over: bool


class PredictionSnapshot(ScatApiModel):
    flight: FlightSummary
    snapshot_time: UtcDateTime
    route_point_count: int
    route: list[PredictionRoutePoint]


class AirportDelayRow(ScatApiModel):
    airport: str = Field(description="Departure airport ICAO code; UNKNOWN when absent.")
    flight_count: int = Field(description="Number of flights departing from this airport.")
    with_atd_count: int = Field(description="Flights with an actual departure time.")
    mean_delay_minutes: float | None = Field(
        default=None, description="Mean ATD-IOBT delay in minutes."
    )
    max_delay_minutes: float | None = Field(default=None)
    min_delay_minutes: float | None = Field(default=None)
    delayed_count: int = Field(description="Flights delayed by 15 minutes or more.")
    delayed_rate: float | None = Field(default=None, description="Delayed flights / flights with ATD.")


class AirportStatsSummary(ScatApiModel):
    flight_count: int
    airport_count: int
    with_atd_count: int
    mean_delay_minutes: float | None = None
    delayed_count: int
    delayed_rate: float | None = None
    delay_threshold_minutes: int = Field(description="Fixed delay threshold in minutes.")


class AirportStats(ScatApiModel):
    statistics: AirportStatsSummary
    airports: list[AirportDelayRow]


class PhaseStatRow(ScatApiModel):
    vertical_mode: int | None = Field(
        default=None, description="Raw I062/200.vert mode: 0 level, 1 climb, 2 descent."
    )
    phase_name: str = Field(description="level, climb, descent, or uncertain.")
    point_count: int
    point_ratio: float | None = None
    start_time: UtcDateTime | None = None
    end_time: UtcDateTime | None = None
    duration_seconds: float | None = None
    min_flight_level: float | None = None
    max_flight_level: float | None = None
    average_flight_level: float | None = None
    average_ground_speed_mps: float | None = None
    maximum_ground_speed_mps: float | None = None
    average_abs_rocd_fpm: float | None = None
    maximum_abs_rocd_fpm: float | None = None


class FlightPhaseStats(ScatApiModel):
    total_points: int
    phases: list[PhaseStatRow]


class PredictionErrorSummary(ScatApiModel):
    mean_m: float | None = None
    median_m: float | None = None
    max_m: float | None = None
    rmse_m: float | None = None


class PredictionErrorStats(ScatApiModel):
    snapshot_time: str | None = Field(
        default=None, description="Timestamp of the SCAT prediction snapshot used."
    )
    route_point_count: int
    track_point_count: int
    matched_fix_count: int
    unmatched_fix_count: int
    statistics: dict[str, Any] = Field(
        description="Aggregated horizontal/vertical/time error statistics."
    )
    fixes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-fix error rows, bounded to 100, ordered by ETO.",
    )
