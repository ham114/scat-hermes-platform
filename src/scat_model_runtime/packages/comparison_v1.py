"""Four-method trajectory comparison package for the ``gbar-align-v1`` protocol."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from scat.prediction.alignment import PredictedPoint, TrackPoint, interpolate_route
from scat.prediction.interfaces import PredictionRequest
from scat.prediction.registry import PredictorRegistry, default_registry
from scat_model_runtime.alignment_core import (
    distance_distribution,
    optional_number,
    trajectory_ade_fde,
    trajectory_horizontal_errors,
)
from scat_model_runtime.contracts import ModelParameterValue
from scat_model_runtime.model_packages import RegressionModelPackage
from scat_model_runtime.packages.gbar_v23 import GBAR_V23_INPUT_UNITS
from scat_model_runtime.task_io import (
    GBAR_ALIGNMENT_PROTOCOL_VERSION,
    ComparisonMethodResult,
    ComparisonMethodSummary,
    ComparisonMetricDistribution,
    ComparisonSampleInput,
    ComparisonSampleResult,
    ComparisonTaskInput,
    ComparisonTaskOutput,
    ComparisonTrajectoryPoint,
    ModelTaskDataError,
    RegressionInputSeries,
    RegressionTaskInput,
)

COMPARISON_V1_MODEL_KEY = "scat-comparison-v1"
COMPARISON_V1_MODEL_VERSION = "1"
CFPL_METHOD_ID = "cfpl"
CFPL_METHOD_VERSION = "cfpl-eto-v1"
PREDICTOR_METHOD_IDS = ("kalman-cv", "constant-velocity-heading")

_METHOD_DISPLAY_NAMES = {
    "scat-gbar-v23": "GBAR v23",
    "kalman-cv": "Kalman 常速度",
    "constant-velocity-heading": "匀速-恒航向",
    CFPL_METHOD_ID: "CFPL 计划航路",
}

__all__ = [
    "CFPL_METHOD_ID",
    "CFPL_METHOD_VERSION",
    "COMPARISON_V1_MODEL_KEY",
    "COMPARISON_V1_MODEL_VERSION",
    "PREDICTOR_METHOD_IDS",
    "ComparisonV1ModelPackage",
]


class _SampleExcluded(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ComparisonV1ModelPackage:
    """Run GBAR, two deterministic predictors and CFPL on one shared sample axis."""

    def __init__(
        self,
        gbar_package: RegressionModelPackage,
        predictor_registry: PredictorRegistry | None = None,
    ) -> None:
        self.gbar_package = gbar_package
        self.predictor_registry = predictor_registry or default_registry

    def start(self) -> None:
        self.gbar_package.start()

    def run(
        self,
        *,
        model_input: ComparisonTaskInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ComparisonTaskOutput:
        unknown_parameters = set(parameters) - {"cutoffs_per_flight"}
        if unknown_parameters:
            raise ValueError(
                f"comparison v1 does not accept runtime parameter: {min(unknown_parameters)}"
            )
        if model_input.protocol_version != GBAR_ALIGNMENT_PROTOCOL_VERSION:
            raise ValueError("comparison v1 requires gbar-align-v1")
        if model_input.feature_units != GBAR_V23_INPUT_UNITS:
            raise ValueError("comparison v1 input units must match GBAR v23")

        regression_input = self._regression_input(model_input)
        gbar_output = self.gbar_package.run(model_input=regression_input, parameters={})
        if len(gbar_output.series) != len(model_input.samples):
            raise RuntimeError("GBAR comparison output count does not match input samples")

        exclusions: Counter[str] = Counter(model_input.excluded_counts)
        results: list[ComparisonSampleResult] = []
        for sample, gbar_series in zip(
            model_input.samples,
            gbar_output.series,
            strict=True,
        ):
            try:
                results.append(
                    self._compare_sample(
                        sample,
                        gbar_series=gbar_series,
                        gbar_model_key=gbar_output.model_key,
                        gbar_model_version=gbar_output.model_version,
                    )
                )
            except _SampleExcluded as exc:
                exclusions[exc.reason] += 1
        if not results:
            raise ModelTaskDataError("统一预测对比没有可用于四方法共同评估的样本。")

        method_summaries = self._summaries(results)
        return ComparisonTaskOutput(
            model_key=COMPARISON_V1_MODEL_KEY,
            model_version=COMPARISON_V1_MODEL_VERSION,
            sample_set_sha256=self._sample_set_hash(results),
            samples=tuple(results),
            method_summaries=method_summaries,
            excluded_counts=dict(sorted(exclusions.items())),
            annotations={
                "comparison_scope": "same_observation_origin_offsets_and_truth",
                "cfpl_interpolation": "forecast_origin_nearest_snapshot_route_eto",
            },
        )

    @staticmethod
    def _regression_input(model_input: ComparisonTaskInput) -> RegressionTaskInput:
        series = []
        for sample in model_input.samples:
            actual = [
                {
                    "timestamp": point.timestamp.isoformat(),
                    "lat": point.latitude_deg,
                    "lon": point.longitude_deg,
                    "measured_flight_level": point.altitude_m,
                }
                for point in sample.actual_future_candidates
            ]
            series.append(
                RegressionInputSeries(
                    input_ref=sample.input_ref,
                    timestamps=sample.observed_timestamps,
                    values=sample.observed_values,
                    source_indices=sample.source_indices,
                    annotations={"actual_future_candidates": actual},
                )
            )
        return RegressionTaskInput(
            feature_names=model_input.feature_names,
            feature_units=model_input.feature_units,
            series=tuple(series),
        )

    def _compare_sample(
        self,
        sample: ComparisonSampleInput,
        *,
        gbar_series: Any,
        gbar_model_key: str,
        gbar_model_version: str,
    ) -> ComparisonSampleResult:
        if gbar_series.forecast_origin != sample.forecast_origin:
            raise RuntimeError("GBAR forecast origin drifted from the comparison sample")
        offsets = tuple(float(value) for value in gbar_series.time_offsets_seconds)
        target_times = tuple(
            sample.forecast_origin + timedelta(seconds=offset) for offset in offsets
        )
        actual_points = self._actual_points(gbar_series, target_times)
        observed = self._observed_track(gbar_series)

        method_results = [
            self._method_result(
                method_id=gbar_model_key,
                method_version=gbar_model_version,
                predicted=self._gbar_points(gbar_series, target_times),
                actual=actual_points,
            )
        ]
        request = PredictionRequest(observed=observed, target_times=target_times)
        for predictor_id in PREDICTOR_METHOD_IDS:
            predictor = self.predictor_registry.get(predictor_id)
            try:
                prediction = predictor.predict(request)
            except ValueError as exc:
                raise _SampleExcluded(f"method_rejected:{predictor_id}") from exc
            predicted = tuple(
                ComparisonTrajectoryPoint(
                    timestamp=point.time,
                    latitude_deg=point.lat,
                    longitude_deg=point.lon,
                    altitude_m=point.altitude_m,
                )
                for point in prediction.fixes
            )
            method_results.append(
                self._method_result(
                    method_id=prediction.predictor_id,
                    method_version=prediction.predictor_version,
                    predicted=predicted,
                    actual=actual_points,
                )
            )
        method_results.append(
            self._method_result(
                method_id=CFPL_METHOD_ID,
                method_version=CFPL_METHOD_VERSION,
                predicted=self._cfpl_points(sample, target_times),
                actual=actual_points,
            )
        )
        return ComparisonSampleResult(
            sample_id=sample.sample_id,
            input_ref=sample.input_ref,
            forecast_origin=sample.forecast_origin,
            time_offsets_seconds=offsets,
            actual_points=actual_points,
            method_results=tuple(method_results),
        )

    @staticmethod
    def _actual_points(
        gbar_series: Any,
        target_times: tuple[Any, ...],
    ) -> tuple[ComparisonTrajectoryPoint, ...]:
        expected = gbar_series.expected_values
        if len(expected) != len(target_times):
            raise _SampleExcluded("truth_missing")
        points = []
        for timestamp, row in zip(target_times, expected, strict=True):
            if len(row) < 2 or row[0] is None or row[1] is None:
                raise _SampleExcluded("truth_missing")
            points.append(
                ComparisonTrajectoryPoint(
                    timestamp=timestamp,
                    latitude_deg=row[0],
                    longitude_deg=row[1],
                    altitude_m=row[2] if len(row) >= 3 else None,
                )
            )
        return tuple(points)

    @staticmethod
    def _observed_track(gbar_series: Any) -> tuple[TrackPoint, ...]:
        raw = gbar_series.annotations.get("observed_trajectory")
        if not isinstance(raw, list):
            raise TypeError("GBAR comparison output is missing observed_trajectory")
        observed = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("GBAR observed_trajectory contains an invalid point")
            offset = optional_number(item.get("time_step_s"))
            lat = optional_number(item.get("lat"))
            lon = optional_number(item.get("lon"))
            if offset is None or lat is None or lon is None:
                raise RuntimeError("GBAR observed_trajectory contains invalid coordinates")
            observed.append(
                TrackPoint(
                    time=gbar_series.forecast_origin + timedelta(seconds=offset),
                    lat=lat,
                    lon=lon,
                    altitude_m=optional_number(item.get("altitude")),
                )
            )
        if len(observed) < 2:
            raise RuntimeError("GBAR observed_trajectory does not contain enough points")
        return tuple(observed)

    @staticmethod
    def _gbar_points(
        gbar_series: Any,
        target_times: tuple[Any, ...],
    ) -> tuple[ComparisonTrajectoryPoint, ...]:
        if len(gbar_series.values) != len(target_times):
            raise RuntimeError("GBAR predictions do not match their offset axis")
        return tuple(
            ComparisonTrajectoryPoint(
                timestamp=timestamp,
                latitude_deg=row[0],
                longitude_deg=row[1],
                altitude_m=row[2] if len(row) >= 3 else None,
            )
            for timestamp, row in zip(target_times, gbar_series.values, strict=True)
        )

    @staticmethod
    def _cfpl_points(
        sample: ComparisonSampleInput,
        target_times: tuple[Any, ...],
    ) -> tuple[ComparisonTrajectoryPoint, ...]:
        route = [
            PredictedPoint(
                time=point.timestamp,
                lat=point.latitude_deg,
                lon=point.longitude_deg,
                altitude_m=point.altitude_m,
            )
            for point in sample.cfpl_points
        ]
        points = []
        for target in target_times:
            point, reason = interpolate_route(route, target)
            if point is None:
                suffix = reason.value if reason is not None else "unavailable"
                raise _SampleExcluded(f"cfpl_{suffix}")
            points.append(
                ComparisonTrajectoryPoint(
                    timestamp=target,
                    latitude_deg=point.lat,
                    longitude_deg=point.lon,
                    altitude_m=point.altitude_m,
                )
            )
        return tuple(points)

    @staticmethod
    def _method_result(
        *,
        method_id: str,
        method_version: str,
        predicted: tuple[ComparisonTrajectoryPoint, ...],
        actual: tuple[ComparisonTrajectoryPoint, ...],
    ) -> ComparisonMethodResult:
        ade, fde = trajectory_ade_fde(
            tuple((point.latitude_deg, point.longitude_deg) for point in predicted),
            tuple((point.latitude_deg, point.longitude_deg) for point in actual),
        )
        horizontal_errors = trajectory_horizontal_errors(
            tuple((point.latitude_deg, point.longitude_deg) for point in predicted),
            tuple((point.latitude_deg, point.longitude_deg) for point in actual),
        )
        return ComparisonMethodResult(
            method_id=method_id,
            method_version=method_version,
            predicted_points=predicted,
            horizontal_errors_metres=horizontal_errors,
            ade_metres=ade,
            fde_metres=fde,
        )

    @staticmethod
    def _summaries(
        samples: list[ComparisonSampleResult],
    ) -> tuple[ComparisonMethodSummary, ...]:
        method_order = [item.method_id for item in samples[0].method_results]
        summaries = []
        for method_id in method_order:
            results = [
                next(item for item in sample.method_results if item.method_id == method_id)
                for sample in samples
            ]
            ade = distance_distribution([item.ade_metres for item in results])
            fde = distance_distribution([item.fde_metres for item in results])
            method_version = results[0].method_version
            if any(item.method_version != method_version for item in results):
                raise RuntimeError(f"comparison method version drifted within run: {method_id}")
            summaries.append(
                ComparisonMethodSummary(
                    method_id=method_id,
                    display_name=_METHOD_DISPLAY_NAMES.get(method_id, method_id),
                    method_version=method_version,
                    sample_count=len(samples),
                    covered_flights=len({item.input_ref.external_id for item in samples}),
                    ade_metres=ComparisonMetricDistribution(
                        mean=ade[0],
                        median=ade[1],
                        p90=ade[2],
                    ),
                    fde_metres=ComparisonMetricDistribution(
                        mean=fde[0],
                        median=fde[1],
                        p90=fde[2],
                    ),
                    version_info={
                        "method_version": method_version,
                        "protocol_version": GBAR_ALIGNMENT_PROTOCOL_VERSION,
                    },
                )
            )
        return tuple(summaries)

    @staticmethod
    def _sample_set_hash(samples: list[ComparisonSampleResult]) -> str:
        payload = [
            {
                "sample_id": sample.sample_id,
                "batch": sample.input_ref.batch,
                "external_id": sample.input_ref.external_id,
                "forecast_origin": sample.forecast_origin.isoformat(),
                "offsets": sample.time_offsets_seconds,
            }
            for sample in sorted(samples, key=lambda item: item.sample_id)
        ]
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def stop(self) -> None:
        self.gbar_package.stop()
