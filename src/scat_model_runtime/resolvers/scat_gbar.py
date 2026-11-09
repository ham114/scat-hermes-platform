"""Resolve one bounded SCAT trajectory into the shared regression task input."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

from scat.preprocessing.gbar_v35 import (
    FLIGHT_LEVEL_TO_METRES,
    FPM_TO_MPS,
    SCAT_GBAR_ENCODING,
    SCAT_GBAR_FEATURES,
)
from scat.repositories.flight_repository import ScatFlightRepository
from scat_model_runtime.contracts import DatasetKind, ModelTaskRequest, ModelTaskType
from scat_model_runtime.packages.gbar_v23 import GBAR_V23_INPUT_UNITS
from scat_model_runtime.task_io import (
    ModelTaskDataError,
    RegressionInputSeries,
    RegressionTaskInput,
)


class ScatGbarRegressionInputResolver:
    """Keep Mongo authorization/querying outside the model package."""

    # GBAR produces 36 roughly ten-second predictions. Reserving the most
    # recent 100 raw SCAT points normally leaves about eight minutes of truth,
    # enough to align the complete prediction horizon despite 5-second input
    # sampling and occasional missing reports.
    automatic_holdout_points = 100

    def __init__(self, repository: ScatFlightRepository | None = None) -> None:
        self.repository = repository or ScatFlightRepository()

    async def resolve(self, *, request: ModelTaskRequest) -> RegressionTaskInput:
        if request.task_type != ModelTaskType.TRAJECTORY_PREDICTION:
            raise ValueError("GBAR resolver only supports trajectory prediction")
        include_actual_future = len(request.references) == 1 or request.evaluator is not None
        series = await asyncio.gather(
            *(
                self.resolve_reference(
                    reference,
                    include_actual_future=include_actual_future,
                )
                for reference in request.references
            )
        )
        return RegressionTaskInput(
            feature_names=SCAT_GBAR_FEATURES,
            feature_units=GBAR_V23_INPUT_UNITS,
            series=tuple(series),
        )

    async def resolve_reference(
        self,
        reference: Any,
        *,
        include_actual_future: bool = True,
    ) -> RegressionInputSeries:
        """Resolve one authorized reference for reuse by higher-level task packages."""
        return await self._resolve_reference_safe(
            reference,
            include_actual_future=include_actual_future,
        )

    async def _resolve_reference_safe(
        self,
        reference: Any,
        *,
        include_actual_future: bool,
    ) -> RegressionInputSeries:
        try:
            return await self._resolve_reference(
                reference,
                include_actual_future=include_actual_future,
            )
        except ModelTaskDataError as exc:
            raise ModelTaskDataError(f"轨迹 {reference.external_id}：{exc}") from exc

    async def _resolve_reference(
        self,
        reference: Any,
        *,
        include_actual_future: bool,
    ) -> RegressionInputSeries:
        if reference.dataset != DatasetKind.SCAT or reference.batch is None:
            raise ValueError("GBAR resolver requires a SCAT batch and external_id")
        if reference.start_time is not None or reference.start != 0:
            raise ValueError("GBAR resolver accepts only an optional end_time")
        if reference.every_nth != 1 or reference.fields or reference.channels:
            raise ValueError("GBAR resolver does not accept sampling or field overrides")

        end_time = None
        automatic_holdout = reference.end_time is None and include_actual_future
        if reference.end_time is not None:
            end_time = (
                reference.end_time.astimezone(UTC)
                .replace(tzinfo=None)
                .isoformat(timespec="microseconds")
            )
        elif automatic_holdout:
            end_time = await self._automatic_backtest_end_time(reference)
        result = await self.repository.get_gbar_input_candidates(
            batch=reference.batch,
            external_id=reference.external_id,
            end_time=end_time,
            limit=reference.limit,
        )
        if result is None or not isinstance(result.get("points"), list):
            raise ModelTaskDataError("没有找到指定的 SCAT 航班。")
        points = result["points"]
        if len(points) < 2:
            raise ModelTaskDataError("没有足够的轨迹点。")
        annotations: dict[str, Any] = {}
        if include_actual_future and end_time is not None:
            actual = await self.repository.get_gbar_future_positions(
                batch=reference.batch,
                external_id=reference.external_id,
                after_time=end_time or "",
                limit=100,
            )
            annotations["actual_future_candidates"] = [
                {
                    "timestamp": self._timestamp(point.get("timestamp")).isoformat(),
                    "lat": self._expected_float("lat", point.get("lat")),
                    "lon": self._expected_float("lon", point.get("lon")),
                    "measured_flight_level": self._expected_float(
                        "measured_flight_level",
                        point.get("measured_flight_level"),
                        FLIGHT_LEVEL_TO_METRES,
                    ),
                    "vx": self._expected_float("vx", point.get("vx")),
                    "vy": self._expected_float("vy", point.get("vy")),
                    "rocd": self._expected_float("rocd", point.get("rocd"), FPM_TO_MPS),
                }
                for point in actual
            ]
            annotations["evaluation_window"] = (
                "automatic_tail_holdout" if automatic_holdout else "explicit_end_time"
            )
            annotations["evaluation_cutoff"] = self._timestamp(end_time).isoformat()
        return RegressionInputSeries(
            input_ref=reference,
            timestamps=tuple(self._timestamp(point.get("timestamp")) for point in points),
            values=tuple(
                tuple(self._optional_float(point.get(name)) for name in SCAT_GBAR_FEATURES)
                for point in points
            ),
            source_indices=tuple(
                self._source_index(point.get("source_index"), index)
                for index, point in enumerate(points)
            ),
            annotations=annotations,
        )

    async def _automatic_backtest_end_time(self, reference: Any) -> str:
        """Choose a bounded historical origin while retaining future truth."""
        tail = await self.repository.get_gbar_input_candidates(
            batch=reference.batch,
            external_id=reference.external_id,
            end_time=None,
            limit=self.automatic_holdout_points,
        )
        points = tail.get("points") if isinstance(tail, dict) else None
        if not isinstance(points, list) or len(points) < self.automatic_holdout_points:
            raise ModelTaskDataError(
                "轨迹点不足，无法同时保留默认误差分析所需的观测段和真实未来段。"
            )
        cutoff = self._timestamp(points[0].get("timestamp"))
        return cutoff.replace(tzinfo=None).isoformat(timespec="microseconds")

    @classmethod
    def _expected_float(
        cls,
        feature: str,
        value: Any,
        multiplier: float = 1.0,
    ) -> float | None:
        number = cls._optional_float(value)
        if number is None:
            return None
        converted = number * multiplier
        encoding = SCAT_GBAR_ENCODING[feature]
        return converted if encoding.minimum <= converted <= encoding.maximum else None

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip())
        else:
            raise ValueError("SCAT轨迹点缺少有效时间戳。")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _source_index(value: Any, fallback: int) -> int:
        if isinstance(value, bool):
            return fallback
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback


__all__ = ["ScatGbarRegressionInputResolver"]
