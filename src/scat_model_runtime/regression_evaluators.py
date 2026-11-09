"""Model-independent deterministic metrics for regression task outputs."""

from __future__ import annotations

import math
from typing import Protocol

from pydantic import Field, field_validator

from scat_model_runtime.component_registry import ComponentRegistry
from scat_model_runtime.contracts import ModelContract
from scat_model_runtime.task_io import ModelTaskDataError, RegressionTaskOutput

EARTH_RADIUS_METRES = 6_371_000.0


class RegressionTargetError(ModelContract):
    target_name: str = Field(min_length=1, max_length=100)
    unit: str = Field(max_length=40)
    sample_count: int = Field(ge=1, le=100_000_000)
    rmse: float = Field(ge=0)
    mae: float = Field(ge=0)

    @field_validator("rmse", "mae")
    @classmethod
    def require_finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("regression metrics must be finite")
        return value


class RegressionTrajectoryError(ModelContract):
    external_id: str = Field(min_length=1, max_length=100)
    batch: str | None = Field(default=None, max_length=200)
    sample_count: int = Field(ge=1, le=100_000)
    ade_metres: float = Field(ge=0)
    fde_metres: float = Field(ge=0)

    @field_validator("ade_metres", "fde_metres")
    @classmethod
    def require_finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("trajectory metrics must be finite")
        return value


class RegressionEvaluation(ModelContract):
    evaluator_key: str = Field(min_length=1, max_length=100)
    evaluator_version: str = Field(min_length=1, max_length=64)
    target_errors: tuple[RegressionTargetError, ...] = Field(min_length=1, max_length=512)
    trajectory_errors: tuple[RegressionTrajectoryError, ...] = Field(
        default_factory=tuple,
        max_length=10_000,
    )
    evaluated_series_count: int = Field(ge=1, le=10_000)
    skipped_series_count: int = Field(ge=0, le=10_000)
    ade_metres: float | None = Field(default=None, ge=0)
    fde_metres: float | None = Field(default=None, ge=0)

    @field_validator("ade_metres", "fde_metres")
    @classmethod
    def require_optional_finite_metric(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("trajectory metrics must be finite")
        return value


class RegressionEvaluator(Protocol):
    """Evaluate any implementation that returns the shared regression contract."""

    def evaluate(self, *, output: RegressionTaskOutput) -> RegressionEvaluation: ...


class RegressionErrorEvaluator:
    """Calculate per-target RMSE/MAE and horizontal geodesic ADE/FDE."""

    key = "regression-errors"
    version = "1"

    def evaluate(self, *, output: RegressionTaskOutput) -> RegressionEvaluation:
        squared_errors: list[list[float]] = [[] for _ in output.target_names]
        absolute_errors: list[list[float]] = [[] for _ in output.target_names]
        trajectory_errors: list[RegressionTrajectoryError] = []
        all_displacements: list[float] = []
        skipped_series = 0

        lat_index = self._target_index(output, "lat")
        lon_index = self._target_index(output, "lon")
        for series in output.series:
            if not series.expected_values:
                skipped_series += 1
                continue
            displacements: list[float] = []
            has_expected_value = False
            for predicted, expected in zip(
                series.values,
                series.expected_values,
                strict=True,
            ):
                for column, (actual, estimate) in enumerate(
                    zip(expected, predicted, strict=True)
                ):
                    if actual is None:
                        continue
                    has_expected_value = True
                    error = estimate - actual
                    squared_errors[column].append(error * error)
                    absolute_errors[column].append(abs(error))
                if lat_index is not None and lon_index is not None:
                    actual_lat = expected[lat_index]
                    actual_lon = expected[lon_index]
                    if actual_lat is not None and actual_lon is not None:
                        displacements.append(
                            self._haversine_metres(
                                predicted[lat_index],
                                predicted[lon_index],
                                actual_lat,
                                actual_lon,
                            )
                        )
            if not has_expected_value:
                skipped_series += 1
                continue
            if displacements:
                all_displacements.extend(displacements)
                trajectory_errors.append(
                    RegressionTrajectoryError(
                        external_id=series.input_ref.external_id,
                        batch=series.input_ref.batch,
                        sample_count=len(displacements),
                        ade_metres=sum(displacements) / len(displacements),
                        fde_metres=displacements[-1],
                    )
                )

        target_errors = tuple(
            RegressionTargetError(
                target_name=target,
                unit=output.target_units[target],
                sample_count=len(errors),
                rmse=math.sqrt(sum(squared_errors[column]) / len(errors)),
                mae=sum(errors) / len(errors),
            )
            for column, (target, errors) in enumerate(
                zip(output.target_names, absolute_errors, strict=True)
            )
            if errors
        )
        if not target_errors:
            raise ModelTaskDataError("历史预测区间没有可与预测点对齐的真实值。")
        fdes = [item.fde_metres for item in trajectory_errors]
        return RegressionEvaluation(
            evaluator_key=self.key,
            evaluator_version=self.version,
            target_errors=target_errors,
            trajectory_errors=tuple(trajectory_errors),
            evaluated_series_count=len(output.series) - skipped_series,
            skipped_series_count=skipped_series,
            ade_metres=(sum(all_displacements) / len(all_displacements) if all_displacements else None),
            fde_metres=(sum(fdes) / len(fdes) if fdes else None),
        )

    @staticmethod
    def _target_index(output: RegressionTaskOutput, target: str) -> int | None:
        try:
            return output.target_names.index(target)
        except ValueError:
            return None

    @staticmethod
    def _haversine_metres(
        predicted_lat: float,
        predicted_lon: float,
        actual_lat: float,
        actual_lon: float,
    ) -> float:
        lat1, lat2 = math.radians(predicted_lat), math.radians(actual_lat)
        delta_lat = lat2 - lat1
        delta_lon = math.radians(actual_lon - predicted_lon)
        a = (
            math.sin(delta_lat / 2.0) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
        )
        return 2.0 * EARTH_RADIUS_METRES * math.asin(math.sqrt(min(1.0, a)))


regression_evaluator_registry = ComponentRegistry[RegressionEvaluator]("regression evaluator")


__all__ = [
    "RegressionErrorEvaluator",
    "RegressionEvaluation",
    "RegressionEvaluator",
    "RegressionTargetError",
    "RegressionTrajectoryError",
    "regression_evaluator_registry",
]
