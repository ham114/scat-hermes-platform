"""Versioned, bounded JSON transport. No application/database objects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)


def iso_timestamp(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("ISO timestamp required")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(iso_timestamp)]
FEATURES = ("lat", "lon", "measured_flight_level", "vx", "vy", "rocd")
INPUT_UNITS = dict(
    zip(
        FEATURES,
        (
            "degree",
            "degree",
            "hundred_feet",
            "metre_per_second",
            "metre_per_second",
            "feet_per_minute",
        ),
        strict=True,
    )
)
OUTPUT_UNITS = dict(
    zip(
        FEATURES,
        (
            "degree",
            "degree",
            "metre",
            "metre_per_second",
            "metre_per_second",
            "metre_per_second",
        ),
        strict=True,
    )
)
MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_BATCH_SIZE = 8
MAX_SERIES_POINTS = 500
MAX_TOTAL_POINTS = 2000
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Finite = Annotated[StrictFloat, Field(allow_inf_nan=False)]
Count = Annotated[StrictInt, Field(ge=0, le=MAX_TOTAL_POINTS)]
Row = tuple[Finite, Finite, Finite, Finite, Finite, Finite]
InputRow = tuple[
    Finite | None, Finite | None, Finite | None, Finite | None, Finite | None, Finite | None
]
ErrorCode = Literal[
    "invalid_input",
    "request_too_large",
    "response_too_large",
    "not_ready",
    "integrity_mismatch",
    "cuda_unavailable",
    "data_unrecoverable",
    "inference_timeout",
    "inference_failed",
    "busy",
    "closed",
]


class TransportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class ExpectedProvenance(TransportModel):
    checkpoint_sha256: Digest
    config_sha256: Digest
    source_tree_sha256: Digest
    schema_sha256: Digest


class ModelProvenance(ExpectedProvenance):
    runtime_sha256: Digest
    service_version: Literal["gbar-v23-service/0.1.0"] = "gbar-v23-service/0.1.0"
    source_uri: str = Field(
        min_length=1, max_length=200, pattern=r"^project-internal://[A-Za-z0-9._/-]+$"
    )


class InputSeries(TransportModel):
    series_id: Identifier
    timestamps: tuple[Timestamp, ...] = Field(min_length=2, max_length=MAX_SERIES_POINTS)
    values: tuple[InputRow, ...] = Field(min_length=2, max_length=MAX_SERIES_POINTS)
    source_indices: tuple[Annotated[StrictInt, Field(ge=0, le=1_000_000)], ...] = Field(
        min_length=2,
        max_length=MAX_SERIES_POINTS,
    )

    @field_validator("timestamps", mode="before")
    @classmethod
    def no_epoch_or_implicit_timezone(cls, value):
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(v, (str, datetime)) for v in value
        ):
            raise ValueError("timestamps must be ISO timestamps with an explicit timezone")
        return value

    @model_validator(mode="after")
    def equal_lengths(self):
        if len(self.timestamps) != len(self.values) or len(self.source_indices) != len(self.values):
            raise ValueError("series lengths differ")
        return self


class Parameters(TransportModel):
    """No runtime overrides are accepted by GBAR v23."""


class InferenceRequest(TransportModel):
    contract_version: Literal["gbar-v23-request/v1"]
    request_id: Identifier
    run_id: Identifier
    model_key: Literal["scat-gbar-v23"]
    model_version: Literal["23"]
    input_schema: Literal["regression-task-input/v1"]
    output_schema: Literal["scat-gbar-v23-prediction/v1"]
    created_at: Timestamp
    feature_names: tuple[str, ...] = Field(min_length=6, max_length=6)
    feature_units: dict[str, str] = Field(min_length=6, max_length=6)
    batch_size: Annotated[StrictInt, Field(ge=1, le=MAX_BATCH_SIZE)]
    series: tuple[InputSeries, ...] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    parameters: Parameters
    expected_provenance: ExpectedProvenance

    @model_validator(mode="after")
    def check_contract(self):
        if self.feature_names != FEATURES or self.feature_units != INPUT_UNITS:
            raise ValueError("feature order or units mismatch")
        if self.batch_size != len(self.series):
            raise ValueError("batch size mismatch")
        if len({s.series_id for s in self.series}) != len(self.series):
            raise ValueError("series IDs must be unique")
        if sum(len(s.values) for s in self.series) > MAX_TOTAL_POINTS:
            raise ValueError("total point limit exceeded")
        return self


class FeatureQuality(TransportModel):
    feature: Literal["lat", "lon", "measured_flight_level", "vx", "vy", "rocd"]
    missing_or_non_finite: Count
    out_of_range: Count
    spike: Count
    interpolated: Count
    extrapolated: Count
    unrepaired: Count


class Quality(TransportModel):
    quality_policy_version: Literal["scat-gbar-v35-quality/v2"]
    source_points_received: Count
    timestamp_or_index_errors: Count
    duplicate_timestamps_removed: Count
    continuity_segments: Count
    hard_gap_count: Count
    selected_segment_points: Count
    source_period_seconds: Annotated[Finite, Field(gt=0, le=15)]
    model_period_seconds: Annotated[Finite, Field(gt=0, le=30)]
    model_ready: Literal[True]
    detection_scope: Literal["bounded_candidate_tail"]
    repair_scope: Literal["latest_continuous_segment_before_window_selection"]
    detected_problem_values: Annotated[StrictInt, Field(ge=0, le=12_000)]
    repaired_values: Annotated[StrictInt, Field(ge=0, le=12_000)]
    unrepaired_values: Annotated[StrictInt, Field(ge=0, le=12_000)]
    features: tuple[FeatureQuality, ...] = Field(min_length=6, max_length=6)

    @field_validator("features")
    @classmethod
    def feature_order(cls, value):
        if tuple(v.feature for v in value) != FEATURES:
            raise ValueError("quality feature order mismatch")
        return value


class ForecastSeries(TransportModel):
    series_id: Identifier
    forecast_origin: Timestamp
    observed_timestamps: tuple[Timestamp, ...] = Field(min_length=36, max_length=36)
    observed_source_indices: tuple[StrictInt, ...] = Field(min_length=36, max_length=36)
    observed_values: tuple[Row, ...] = Field(min_length=36, max_length=36)
    time_offsets_seconds: tuple[Finite, ...] = Field(min_length=36, max_length=36)
    values: tuple[Row, ...] = Field(min_length=36, max_length=36)
    class_probabilities: tuple[Annotated[Finite, Field(ge=0, le=1)], ...] | None = Field(
        default=None,
        min_length=10,
        max_length=10,
    )
    quality: Quality

    @model_validator(mode="after")
    def time_axis(self):
        if self.forecast_origin != self.observed_timestamps[-1]:
            raise ValueError("forecast origin mismatch")
        if any(a >= b for a, b in zip(self.observed_timestamps, self.observed_timestamps[1:])):
            raise ValueError("observed timestamps must increase")
        expected = tuple(round(self.quality.model_period_seconds * i, 3) for i in range(1, 37))
        if self.time_offsets_seconds != expected:
            raise ValueError("forecast offsets mismatch")
        if self.class_probabilities is not None and abs(sum(self.class_probabilities) - 1) > 0.001:
            raise ValueError("class probabilities must sum to one")
        return self


class InferenceResponse(TransportModel):
    contract_version: Literal["gbar-v23-response/v1"] = "gbar-v23-response/v1"
    request_id: Identifier
    run_id: Identifier
    model_key: Literal["scat-gbar-v23"] = "scat-gbar-v23"
    model_version: Literal["23"] = "23"
    input_schema: Literal["regression-task-input/v1"] = "regression-task-input/v1"
    output_schema: Literal["scat-gbar-v23-prediction/v1"] = "scat-gbar-v23-prediction/v1"
    completed_at: Timestamp
    target_names: tuple[str, ...] = Field(min_length=6, max_length=6)
    target_units: dict[str, str] = Field(min_length=6, max_length=6)
    batch_size: Annotated[StrictInt, Field(ge=1, le=MAX_BATCH_SIZE)]
    series: tuple[ForecastSeries, ...] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    provenance: ModelProvenance

    @model_validator(mode="after")
    def output_contract(self):
        if self.target_names != FEATURES or self.target_units != OUTPUT_UNITS:
            raise ValueError("output features or units mismatch")
        if (
            self.batch_size != len(self.series)
            or len({v.series_id for v in self.series}) != self.batch_size
        ):
            raise ValueError("output batch mismatch")
        return self


class ErrorDetail(TransportModel):
    code: ErrorCode
    message: Literal["GBAR request could not be completed."] = (
        "GBAR request could not be completed."
    )
    retryable: Literal[False] = False
    execution_state: Literal["not_started", "unknown"]


class ErrorResponse(TransportModel):
    contract_version: Literal["gbar-v23-error/v1"] = "gbar-v23-error/v1"
    request_id: Identifier | None = None
    run_id: Identifier | None = None
    error: ErrorDetail


def schema_digest() -> str:
    schemas = {
        m.__name__: m.model_json_schema()
        for m in (InferenceRequest, InferenceResponse, ErrorResponse)
    }
    return hashlib.sha256(
        json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parse_request(raw: bytes) -> InferenceRequest:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request byte limit exceeded")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("JSON constants must be finite")

    data = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    return InferenceRequest.model_validate(data)
