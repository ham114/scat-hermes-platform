"""Task-level model I/O contracts shared by implementations of the same task."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from scat_model_runtime.contracts import DatasetKind, ModelContract, ModelInputReference

REGRESSION_TASK_INPUT_SCHEMA = "regression-task-input/v1"
REGRESSION_TASK_OUTPUT_SCHEMA = "regression-task-output/v1"
COMPARISON_TASK_INPUT_SCHEMA: Literal["comparison-task-input/v1"] = "comparison-task-input/v1"
COMPARISON_TASK_OUTPUT_SCHEMA: Literal["comparison-task-output/v1"] = "comparison-task-output/v1"
GBAR_ALIGNMENT_PROTOCOL_VERSION: Literal["gbar-align-v1"] = "gbar-align-v1"

_COMPARISON_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,99}$")


class ModelTaskDataError(ValueError):
    """A bounded, user-safe data error that may cross the model-agent boundary."""


class RegressionInputSeries(ModelContract):
    """One canonical, model-independent input sequence."""

    input_ref: ModelInputReference
    timestamps: tuple[datetime, ...] = Field(min_length=2, max_length=1_000_000)
    values: tuple[tuple[float | None, ...], ...] = Field(
        min_length=2,
        max_length=1_000_000,
    )
    source_indices: tuple[int, ...] = Field(default_factory=tuple, max_length=1_000_000)
    annotations: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("timestamps")
    @classmethod
    def require_timezone(cls, value: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(item.tzinfo is None or item.utcoffset() is None for item in value):
            raise ValueError("regression input timestamps must include a timezone")
        return value

    @field_validator("values")
    @classmethod
    def require_finite_present_values(
        cls,
        value: tuple[tuple[float | None, ...], ...],
    ) -> tuple[tuple[float | None, ...], ...]:
        if any(item is not None and not math.isfinite(item) for row in value for item in row):
            raise ValueError("regression input values must be finite or null")
        return value

    @model_validator(mode="after")
    def validate_lengths(self) -> RegressionInputSeries:
        if len(self.timestamps) != len(self.values):
            raise ValueError("regression input timestamps and values must have equal length")
        if self.source_indices and len(self.source_indices) != len(self.values):
            raise ValueError("regression input source_indices must match values length")
        return self


class RegressionTaskInput(ModelContract):
    """Unified input boundary for sequence/scalar regression model packages."""

    schema_version: Literal["regression-task-input/v1"] = "regression-task-input/v1"
    feature_names: tuple[str, ...] = Field(min_length=1, max_length=512)
    feature_units: dict[str, str] = Field(min_length=1, max_length=512)
    series: tuple[RegressionInputSeries, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_feature_contract(self) -> RegressionTaskInput:
        if len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("regression feature names must be unique")
        if set(self.feature_units) != set(self.feature_names):
            raise ValueError("regression feature units must match feature names")
        width = len(self.feature_names)
        if any(len(row) != width for item in self.series for row in item.values):
            raise ValueError("regression input rows must match feature_names width")
        datasets = {item.input_ref.dataset for item in self.series}
        if len(datasets) != 1:
            raise ValueError("one regression task input cannot mix datasets")
        return self

    @property
    def batch_size(self) -> int:
        return len(self.series)


class RegressionPredictionSeries(ModelContract):
    """One prediction sequence represented by one origin and relative seconds."""

    input_ref: ModelInputReference
    forecast_origin: datetime
    time_offsets_seconds: tuple[float, ...] = Field(min_length=1, max_length=100_000)
    values: tuple[tuple[float, ...], ...] = Field(min_length=1, max_length=100_000)
    expected_values: tuple[tuple[float | None, ...], ...] = Field(
        default_factory=tuple,
        max_length=100_000,
    )
    annotations: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("forecast_origin")
    @classmethod
    def require_origin_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("regression forecast origin must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_prediction_axis(self) -> RegressionPredictionSeries:
        if len(self.time_offsets_seconds) != len(self.values):
            raise ValueError("regression output offsets and values must have equal length")
        if self.expected_values and len(self.expected_values) != len(self.values):
            raise ValueError("regression expected and predicted values must have equal length")
        previous = 0.0
        for offset in self.time_offsets_seconds:
            if not math.isfinite(offset) or offset <= previous:
                raise ValueError("regression output offsets must be finite and increasing")
            previous = offset
        if any(not math.isfinite(value) for row in self.values for value in row):
            raise ValueError("regression output values must be finite")
        if any(
            value is not None and not math.isfinite(value)
            for row in self.expected_values
            for value in row
        ):
            raise ValueError("regression expected values must be finite or null")
        return self


class RegressionTaskOutput(ModelContract):
    """Unified business output returned by every registered regression package."""

    schema_version: Literal["regression-task-output/v1"] = "regression-task-output/v1"
    model_key: str = Field(min_length=1, max_length=100)
    model_version: str = Field(min_length=1, max_length=64)
    target_names: tuple[str, ...] = Field(min_length=1, max_length=512)
    target_units: dict[str, str] = Field(min_length=1, max_length=512)
    series: tuple[RegressionPredictionSeries, ...] = Field(
        min_length=1,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def validate_target_contract(self) -> RegressionTaskOutput:
        if len(set(self.target_names)) != len(self.target_names):
            raise ValueError("regression target names must be unique")
        if set(self.target_units) != set(self.target_names):
            raise ValueError("regression target units must match target names")
        width = len(self.target_names)
        if any(len(row) != width for item in self.series for row in item.values):
            raise ValueError("regression output rows must match target_names width")
        if any(len(row) != width for item in self.series for row in item.expected_values):
            raise ValueError("regression expected rows must match target_names width")
        return self

    @property
    def batch_size(self) -> int:
        return len(self.series)


class ComparisonTrajectoryPoint(ModelContract):
    """One timestamped WGS84 position used by the comparison boundary."""

    timestamp: datetime
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=180)
    altitude_m: float | None = None

    @field_validator("timestamp")
    @classmethod
    def require_timestamp_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("comparison point timestamps must include a timezone")
        return value

    @field_validator("latitude_deg", "longitude_deg", "altitude_m")
    @classmethod
    def require_finite_coordinates(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("comparison point coordinates must be finite")
        return value


class ComparisonSampleInput(ModelContract):
    """One flight/cutoff sample shared unchanged by every compared method."""

    sample_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    )
    input_ref: ModelInputReference
    forecast_origin: datetime
    observed_timestamps: tuple[datetime, ...] = Field(min_length=2, max_length=10_000)
    observed_values: tuple[tuple[float | None, ...], ...] = Field(
        min_length=2,
        max_length=10_000,
    )
    source_indices: tuple[int, ...] = Field(default_factory=tuple, max_length=10_000)
    actual_future_candidates: tuple[ComparisonTrajectoryPoint, ...] = Field(
        min_length=1,
        max_length=100_000,
    )
    cfpl_points: tuple[ComparisonTrajectoryPoint, ...] = Field(
        min_length=2,
        max_length=100_000,
    )
    annotations: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("forecast_origin")
    @classmethod
    def require_origin_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("comparison forecast origin must include a timezone")
        return value

    @field_validator("observed_timestamps")
    @classmethod
    def require_observed_time_axis(
        cls,
        value: tuple[datetime, ...],
    ) -> tuple[datetime, ...]:
        if any(item.tzinfo is None or item.utcoffset() is None for item in value):
            raise ValueError("comparison observed timestamps must include a timezone")
        if any(current <= previous for previous, current in pairwise(value)):
            raise ValueError("comparison observed timestamps must be strictly increasing")
        return value

    @field_validator("observed_values")
    @classmethod
    def require_finite_observed_values(
        cls,
        value: tuple[tuple[float | None, ...], ...],
    ) -> tuple[tuple[float | None, ...], ...]:
        if any(item is not None and not math.isfinite(item) for row in value for item in row):
            raise ValueError("comparison observed values must be finite or null")
        return value

    @model_validator(mode="after")
    def validate_sample_axes(self) -> ComparisonSampleInput:
        if self.input_ref.dataset != DatasetKind.SCAT:
            raise ValueError("comparison samples currently require the SCAT dataset")
        if len(self.observed_timestamps) != len(self.observed_values):
            raise ValueError("comparison observed timestamps and values must have equal length")
        if self.source_indices and len(self.source_indices) != len(self.observed_values):
            raise ValueError("comparison source_indices must match observed values length")
        if self.observed_timestamps[-1] != self.forecast_origin:
            raise ValueError("comparison forecast origin must equal the final observed timestamp")
        for name, points in (
            ("actual future", self.actual_future_candidates),
            ("CFPL", self.cfpl_points),
        ):
            if any(
                current.timestamp <= previous.timestamp for previous, current in pairwise(points)
            ):
                raise ValueError(f"comparison {name} timestamps must be strictly increasing")
        if self.actual_future_candidates[0].timestamp <= self.forecast_origin:
            raise ValueError("comparison actual future must start after the forecast origin")
        if self.cfpl_points[-1].timestamp <= self.forecast_origin:
            raise ValueError("comparison CFPL must extend beyond the forecast origin")
        return self


class ComparisonTaskInput(ModelContract):
    """Strict input for one common-condition multi-method comparison run."""

    schema_version: Literal["comparison-task-input/v1"] = COMPARISON_TASK_INPUT_SCHEMA
    protocol_version: Literal["gbar-align-v1"] = GBAR_ALIGNMENT_PROTOCOL_VERSION
    feature_names: tuple[str, ...] = Field(min_length=1, max_length=512)
    feature_units: dict[str, str] = Field(min_length=1, max_length=512)
    samples: tuple[ComparisonSampleInput, ...] = Field(min_length=1, max_length=10_000)
    excluded_counts: dict[str, int] = Field(default_factory=dict, max_length=100)

    @field_validator("excluded_counts")
    @classmethod
    def validate_excluded_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            _COMPARISON_KEY_PATTERN.fullmatch(key) is None or count < 0
            for key, count in value.items()
        ):
            raise ValueError("comparison excluded_counts must use bounded keys and counts")
        return value

    @model_validator(mode="after")
    def validate_feature_contract(self) -> ComparisonTaskInput:
        if len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("comparison feature names must be unique")
        if set(self.feature_units) != set(self.feature_names):
            raise ValueError("comparison feature units must match feature names")
        width = len(self.feature_names)
        if any(len(row) != width for sample in self.samples for row in sample.observed_values):
            raise ValueError("comparison observed rows must match feature_names width")
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("comparison sample ids must be unique")
        return self

    @property
    def batch_size(self) -> int:
        return len(self.samples)


class ComparisonMetricDistribution(ModelContract):
    """Aggregate distribution for one non-negative distance metric."""

    mean: float = Field(ge=0)
    median: float = Field(ge=0)
    p90: float = Field(ge=0)

    @field_validator("mean", "median", "p90")
    @classmethod
    def require_finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("comparison metrics must be finite")
        return value


class ComparisonMethodResult(ModelContract):
    """One method's prediction on the sample's shared output axis."""

    method_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    )
    method_version: str = Field(min_length=1, max_length=100)
    predicted_points: tuple[ComparisonTrajectoryPoint, ...] = Field(
        min_length=1,
        max_length=100_000,
    )
    horizontal_errors_metres: tuple[float, ...] = Field(
        default_factory=tuple,
        max_length=100_000,
    )
    ade_metres: float = Field(ge=0)
    fde_metres: float = Field(ge=0)
    annotations: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("ade_metres", "fde_metres")
    @classmethod
    def require_finite_error(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("comparison errors must be finite")
        return value

    @field_validator("horizontal_errors_metres")
    @classmethod
    def require_finite_point_errors(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(error < 0 or not math.isfinite(error) for error in value):
            raise ValueError("comparison point errors must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_point_errors(self) -> ComparisonMethodResult:
        # Empty preserves compatibility with outputs persisted before M7-07.
        if self.horizontal_errors_metres and len(self.horizontal_errors_metres) != len(
            self.predicted_points
        ):
            raise ValueError("comparison point errors must align with predicted points")
        if self.horizontal_errors_metres:
            expected_ade = sum(self.horizontal_errors_metres) / len(self.horizontal_errors_metres)
            if not math.isclose(self.ade_metres, expected_ade, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("comparison ADE must equal the mean point error")
            if not math.isclose(
                self.fde_metres,
                self.horizontal_errors_metres[-1],
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                raise ValueError("comparison FDE must equal the final point error")
        return self


class ComparisonSampleResult(ModelContract):
    """All method outputs for one immutable sample and shared time axis."""

    sample_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    )
    input_ref: ModelInputReference
    forecast_origin: datetime
    time_offsets_seconds: tuple[float, ...] = Field(min_length=1, max_length=100_000)
    actual_points: tuple[ComparisonTrajectoryPoint, ...] = Field(
        min_length=1,
        max_length=100_000,
    )
    method_results: tuple[ComparisonMethodResult, ...] = Field(
        min_length=1,
        max_length=20,
    )

    @field_validator("forecast_origin")
    @classmethod
    def require_result_origin_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("comparison result origin must include a timezone")
        return value

    @field_validator("time_offsets_seconds")
    @classmethod
    def validate_offsets(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        previous = 0.0
        for offset in value:
            if not math.isfinite(offset) or offset <= previous:
                raise ValueError("comparison offsets must be finite and increasing")
            previous = offset
        return value

    @model_validator(mode="after")
    def validate_shared_axis(self) -> ComparisonSampleResult:
        expected_timestamps = tuple(
            self.forecast_origin + timedelta(seconds=offset) for offset in self.time_offsets_seconds
        )
        if tuple(point.timestamp for point in self.actual_points) != expected_timestamps:
            raise ValueError("comparison actual points must use the shared output axis")
        method_ids = [item.method_id for item in self.method_results]
        if len(method_ids) != len(set(method_ids)):
            raise ValueError("comparison method results must be unique")
        if any(
            tuple(point.timestamp for point in item.predicted_points) != expected_timestamps
            for item in self.method_results
        ):
            raise ValueError("comparison predictions must use the shared output axis")
        return self


class ComparisonMethodSummary(ModelContract):
    """Traceable aggregate metrics for one compared method."""

    method_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    method_version: str = Field(min_length=1, max_length=100)
    sample_count: int = Field(ge=1)
    covered_flights: int = Field(ge=1)
    ade_metres: ComparisonMetricDistribution
    fde_metres: ComparisonMetricDistribution
    version_info: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("version_info")
    @classmethod
    def validate_version_info(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            _COMPARISON_KEY_PATTERN.fullmatch(key) is None or not item or len(item) > 200
            for key, item in value.items()
        ):
            raise ValueError("comparison version_info must contain bounded version strings")
        return value


class ComparisonTaskOutput(ModelContract):
    """Unified output returned by every registered comparison package."""

    schema_version: Literal["comparison-task-output/v1"] = COMPARISON_TASK_OUTPUT_SCHEMA
    protocol_version: Literal["gbar-align-v1"] = GBAR_ALIGNMENT_PROTOCOL_VERSION
    model_key: str = Field(min_length=1, max_length=100)
    model_version: str = Field(min_length=1, max_length=64)
    sample_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    samples: tuple[ComparisonSampleResult, ...] = Field(min_length=1, max_length=10_000)
    method_summaries: tuple[ComparisonMethodSummary, ...] = Field(
        min_length=1,
        max_length=20,
    )
    excluded_counts: dict[str, int] = Field(default_factory=dict, max_length=100)
    annotations: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("excluded_counts")
    @classmethod
    def validate_excluded_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            _COMPARISON_KEY_PATTERN.fullmatch(key) is None or count < 0
            for key, count in value.items()
        ):
            raise ValueError("comparison excluded_counts must use bounded keys and counts")
        return value

    @model_validator(mode="after")
    def validate_method_and_sample_sets(self) -> ComparisonTaskOutput:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("comparison output sample ids must be unique")
        summary_ids = [summary.method_id for summary in self.method_summaries]
        if len(summary_ids) != len(set(summary_ids)):
            raise ValueError("comparison method summaries must be unique")
        expected_methods = set(summary_ids)
        if any(
            {method.method_id for method in sample.method_results} != expected_methods
            for sample in self.samples
        ):
            raise ValueError("every comparison sample must contain the summarized methods")
        if any(summary.sample_count != len(self.samples) for summary in self.method_summaries):
            raise ValueError("comparison summary sample_count must match output samples")
        return self

    @property
    def batch_size(self) -> int:
        return len(self.samples)


__all__ = [
    "COMPARISON_TASK_INPUT_SCHEMA",
    "COMPARISON_TASK_OUTPUT_SCHEMA",
    "GBAR_ALIGNMENT_PROTOCOL_VERSION",
    "REGRESSION_TASK_INPUT_SCHEMA",
    "REGRESSION_TASK_OUTPUT_SCHEMA",
    "ComparisonMethodResult",
    "ComparisonMethodSummary",
    "ComparisonMetricDistribution",
    "ComparisonSampleInput",
    "ComparisonSampleResult",
    "ComparisonTaskInput",
    "ComparisonTaskOutput",
    "ComparisonTrajectoryPoint",
    "ModelTaskDataError",
    "RegressionInputSeries",
    "RegressionPredictionSeries",
    "RegressionTaskInput",
    "RegressionTaskOutput",
]
