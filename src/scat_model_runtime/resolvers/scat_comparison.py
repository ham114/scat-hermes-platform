"""Resolve bounded SCAT flight references into ``gbar-align-v1`` comparison samples."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from scat.prediction.alignment import PredictedPoint, build_route
from scat.preprocessing.gbar_v35 import SCAT_GBAR_FEATURES
from scat.repositories.flight_repository import ScatFlightRepository
from scat_model_runtime.contracts import DatasetKind, ModelTaskRequest, ModelTaskType
from scat_model_runtime.packages.gbar_v23 import GBAR_V23_INPUT_UNITS
from scat_model_runtime.resolvers.scat_gbar import ScatGbarRegressionInputResolver
from scat_model_runtime.task_io import (
    ComparisonSampleInput,
    ComparisonTaskInput,
    ComparisonTrajectoryPoint,
    ModelTaskDataError,
)

COMPARISON_DEFAULT_CUTOFFS_PER_FLIGHT = 3
COMPARISON_MAX_CUTOFFS_PER_FLIGHT = 3
COMPARISON_MIN_OBSERVATION_POINTS = 71
COMPARISON_TRUTH_POINTS = 100
COMPARISON_SCAN_POINTS = 2_000

__all__ = [
    "COMPARISON_DEFAULT_CUTOFFS_PER_FLIGHT",
    "COMPARISON_MAX_CUTOFFS_PER_FLIGHT",
    "COMPARISON_MIN_OBSERVATION_POINTS",
    "COMPARISON_SCAN_POINTS",
    "COMPARISON_TRUTH_POINTS",
    "ScatComparisonInputResolver",
]


class _ComparisonDataUnavailable(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ScatComparisonInputResolver:
    """Create common-condition samples without exposing database payloads to the agent."""

    def __init__(
        self,
        repository: ScatFlightRepository | None = None,
        gbar_resolver: ScatGbarRegressionInputResolver | None = None,
    ) -> None:
        self.repository = repository or ScatFlightRepository()
        self.gbar_resolver = gbar_resolver or ScatGbarRegressionInputResolver(self.repository)

    async def resolve(self, *, request: ModelTaskRequest) -> ComparisonTaskInput:
        if request.task_type != ModelTaskType.MODEL_COMPARISON:
            raise ValueError("SCAT comparison resolver only supports model_comparison")
        cutoffs_per_flight = self._cutoff_count(request.parameters)
        exclusions: Counter[str] = Counter()
        samples: list[ComparisonSampleInput] = []
        for reference in request.references:
            self._validate_reference(reference)
            try:
                cutoffs = await self._cutoff_times(reference, cutoffs_per_flight)
            except _ComparisonDataUnavailable as exc:
                exclusions[exc.reason] += cutoffs_per_flight
                continue
            for cutoff in cutoffs:
                try:
                    samples.append(await self._resolve_sample(reference, cutoff))
                except _ComparisonDataUnavailable as exc:
                    exclusions[exc.reason] += 1
        if not samples:
            raise ModelTaskDataError("没有航班能形成统一预测对比所需的观测、真值和CFPL样本。")
        return ComparisonTaskInput(
            feature_names=SCAT_GBAR_FEATURES,
            feature_units=GBAR_V23_INPUT_UNITS,
            samples=tuple(samples),
            excluded_counts=dict(sorted(exclusions.items())),
        )

    @staticmethod
    def _cutoff_count(parameters: dict[str, Any]) -> int:
        unknown = set(parameters) - {"cutoffs_per_flight"}
        if unknown:
            raise ValueError(f"comparison parameters are not supported: {min(unknown)}")
        raw = parameters.get("cutoffs_per_flight", COMPARISON_DEFAULT_CUTOFFS_PER_FLIGHT)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError("cutoffs_per_flight must be an integer")
        if not 1 <= raw <= COMPARISON_MAX_CUTOFFS_PER_FLIGHT:
            raise ValueError(
                f"cutoffs_per_flight must be between 1 and {COMPARISON_MAX_CUTOFFS_PER_FLIGHT}"
            )
        return raw

    @staticmethod
    def _validate_reference(reference: Any) -> None:
        if reference.dataset != DatasetKind.SCAT or reference.batch is None:
            raise ValueError("SCAT comparison requires batch-scoped SCAT references")
        if reference.start_time is not None or reference.start != 0:
            raise ValueError("SCAT comparison does not accept start offsets")
        if reference.every_nth != 1 or reference.fields or reference.channels:
            raise ValueError("SCAT comparison does not accept sampling or field overrides")
        if reference.limit < COMPARISON_MIN_OBSERVATION_POINTS:
            raise ValueError(
                f"SCAT comparison requires at least {COMPARISON_MIN_OBSERVATION_POINTS} source points"
            )

    async def _cutoff_times(self, reference: Any, count: int) -> tuple[datetime, ...]:
        if reference.end_time is not None:
            return (reference.end_time.astimezone(UTC),)
        result = await self.repository.get_gbar_input_candidates(
            batch=reference.batch,
            external_id=reference.external_id,
            end_time=None,
            limit=COMPARISON_SCAN_POINTS,
        )
        raw_points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(raw_points, list):
            raise _ComparisonDataUnavailable("observation_window_unavailable")
        timestamps = [self._timestamp(item.get("timestamp")) for item in raw_points]
        first = COMPARISON_MIN_OBSERVATION_POINTS - 1
        last = len(timestamps) - COMPARISON_TRUTH_POINTS - 1
        if last < first or last - first + 1 < count:
            raise _ComparisonDataUnavailable("observation_window_unavailable")
        indices: tuple[int, ...]
        if count == 1:
            indices = (last,)
        else:
            span = last - first
            indices = tuple(first + round(index * span / (count - 1)) for index in range(count))
        return tuple(timestamps[index] for index in indices)

    async def _resolve_sample(self, reference: Any, cutoff: datetime) -> ComparisonSampleInput:
        cutoff_reference = reference.model_copy(update={"end_time": cutoff})
        try:
            series = await self.gbar_resolver.resolve_reference(
                cutoff_reference,
                include_actual_future=True,
            )
        except ModelTaskDataError as exc:
            raise _ComparisonDataUnavailable("observation_window_unavailable") from exc
        forecast_origin = series.timestamps[-1]
        actual = self._actual_future(series.annotations.get("actual_future_candidates"))
        snapshot = await self.repository.get_prediction_snapshot_at_or_before(
            batch=reference.batch,
            external_id=reference.external_id,
            timestamp=self._database_timestamp(forecast_origin),
        )
        cfpl, snapshot_time = self._cfpl_points(snapshot, forecast_origin=forecast_origin)
        return ComparisonSampleInput(
            sample_id=self._sample_id(reference.external_id, forecast_origin),
            input_ref=cutoff_reference,
            forecast_origin=forecast_origin,
            observed_timestamps=series.timestamps,
            observed_values=series.values,
            source_indices=series.source_indices,
            actual_future_candidates=actual,
            cfpl_points=cfpl,
            annotations={
                "cutoff_selection": (
                    "explicit_end_time" if reference.end_time is not None else "evenly_spaced"
                ),
                "cfpl_snapshot_time": snapshot_time,
            },
        )

    @classmethod
    def _actual_future(cls, raw: Any) -> tuple[ComparisonTrajectoryPoint, ...]:
        if not isinstance(raw, list):
            raise _ComparisonDataUnavailable("truth_unavailable")
        points = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            lat = cls._number(item.get("lat"))
            lon = cls._number(item.get("lon"))
            if lat is None or lon is None:
                continue
            points.append(
                ComparisonTrajectoryPoint(
                    timestamp=cls._timestamp(item.get("timestamp")),
                    latitude_deg=lat,
                    longitude_deg=lon,
                    altitude_m=cls._number(item.get("measured_flight_level")),
                )
            )
        if not points:
            raise _ComparisonDataUnavailable("truth_unavailable")
        return tuple(points)

    @classmethod
    def _cfpl_points(
        cls,
        result: Any,
        *,
        forecast_origin: datetime,
    ) -> tuple[tuple[ComparisonTrajectoryPoint, ...], str]:
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        if not isinstance(snapshot, dict):
            raise _ComparisonDataUnavailable("cfpl_unavailable")
        route, reason = build_route(snapshot.get("route") or [])
        deduplicated: list[PredictedPoint] = []
        for point in route:
            if deduplicated and deduplicated[-1].time == point.time:
                deduplicated[-1] = point
            else:
                deduplicated.append(point)
        if reason is not None or len(deduplicated) < 2 or deduplicated[-1].time <= forecast_origin:
            raise _ComparisonDataUnavailable("cfpl_unavailable")
        points = tuple(
            ComparisonTrajectoryPoint(
                timestamp=point.time,
                latitude_deg=point.lat,
                longitude_deg=point.lon,
                altitude_m=point.altitude_m,
            )
            for point in deduplicated
        )
        snapshot_time = cls._timestamp(snapshot.get("time_stamp")).isoformat()
        return points, snapshot_time

    @staticmethod
    def _sample_id(external_id: str, cutoff: datetime) -> str:
        payload = f"{external_id}|{cutoff.astimezone(UTC).isoformat()}".encode()
        return f"sample-{hashlib.sha256(payload).hexdigest()[:20]}"

    @staticmethod
    def _database_timestamp(value: datetime) -> str:
        return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds")

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip())
        else:
            raise _ComparisonDataUnavailable("invalid_timestamp")
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
