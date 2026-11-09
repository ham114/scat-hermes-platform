"""SCAT-to-GBAR v35 preprocessing with repair-before-window semantics."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

FEET_TO_METRES = 0.3048
FPM_TO_MPS = FEET_TO_METRES / 60.0
FLIGHT_LEVEL_TO_METRES = 100.0 * FEET_TO_METRES
SCAT_GBAR_FEATURES = ("lat", "lon", "measured_flight_level", "vx", "vy", "rocd")
SCAT_GBAR_QUALITY_POLICY_VERSION = "scat-gbar-v35-quality/v2"
SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS = 15.0
SCAT_GBAR_MAXIMUM_REPAIR_RUN = 3
SCAT_GBAR_MAXIMUM_REPAIR_SPAN_SECONDS = 30.0
SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES = 4_000.0
SCAT_GBAR_SPIKE_THRESHOLDS: dict[str, float] = {
    "measured_flight_level": 600.0,
    "vx": 80.0,
    "vy": 80.0,
    "rocd": 20.0,
}


@dataclass(frozen=True, slots=True)
class FeatureEncoding:
    bits: int
    minimum: float
    maximum: float
    delta_minimum: float
    delta_maximum: float


SCAT_GBAR_ENCODING: dict[str, FeatureEncoding] = {
    "lat": FeatureEncoding(16, 55.0, 70.0, -200.0, 200.0),
    "lon": FeatureEncoding(16, 10.0, 25.0, -400.0, 400.0),
    "measured_flight_level": FeatureEncoding(15, -100.0, 14_000.0, -1200.0, 1200.0),
    "vx": FeatureEncoding(10, -400.0, 400.0, -200.0, 200.0),
    "vy": FeatureEncoding(10, -400.0, 400.0, -200.0, 200.0),
    "rocd": FeatureEncoding(8, -125.0, 60.0, -100.0, 100.0),
}


class ScatGbarDataError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        quality: ScatGbarQualityReport | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.quality = quality


@dataclass(frozen=True, slots=True)
class ScatGbarFeatureQuality:
    feature: str
    missing_or_non_finite: int = 0
    out_of_range: int = 0
    spike: int = 0
    interpolated: int = 0
    extrapolated: int = 0
    unrepaired: int = 0

    def as_dict(self) -> dict[str, int | str]:
        return {
            "feature": self.feature,
            "missing_or_non_finite": self.missing_or_non_finite,
            "out_of_range": self.out_of_range,
            "spike": self.spike,
            "interpolated": self.interpolated,
            "extrapolated": self.extrapolated,
            "unrepaired": self.unrepaired,
        }


@dataclass(frozen=True, slots=True)
class ScatGbarQualityReport:
    source_points_received: int
    timestamp_or_index_errors: int
    duplicate_timestamps_removed: int
    continuity_segments: int
    hard_gap_count: int
    selected_segment_points: int
    source_period_seconds: float
    model_period_seconds: float
    model_ready: bool
    feature_quality: tuple[ScatGbarFeatureQuality, ...]

    @property
    def repaired_values(self) -> int:
        return sum(item.interpolated + item.extrapolated for item in self.feature_quality)

    @property
    def detected_problem_values(self) -> int:
        return sum(
            item.missing_or_non_finite + item.out_of_range + item.spike
            for item in self.feature_quality
        )

    @property
    def unrepaired_values(self) -> int:
        return sum(item.unrepaired for item in self.feature_quality)

    def as_dict(self) -> dict[str, Any]:
        return {
            "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
            "source_points_received": self.source_points_received,
            "timestamp_or_index_errors": self.timestamp_or_index_errors,
            "duplicate_timestamps_removed": self.duplicate_timestamps_removed,
            "continuity_segments": self.continuity_segments,
            "hard_gap_count": self.hard_gap_count,
            "selected_segment_points": self.selected_segment_points,
            "source_period_seconds": self.source_period_seconds,
            "model_period_seconds": self.model_period_seconds,
            "model_ready": self.model_ready,
            "detection_scope": "bounded_candidate_tail",
            "repair_scope": "latest_continuous_segment_before_window_selection",
            "detected_problem_values": self.detected_problem_values,
            "repaired_values": self.repaired_values,
            "unrepaired_values": self.unrepaired_values,
            "features": [item.as_dict() for item in self.feature_quality],
        }


@dataclass(frozen=True, slots=True)
class ScatGbarPreparedWindow:
    batch: str
    external_id: str
    feature_names: tuple[str, ...]
    source_indices: tuple[int, ...]
    timestamps: tuple[datetime, ...]
    time_offsets_seconds: tuple[float, ...]
    feature_values: np.ndarray
    absolute_binary: np.ndarray
    interval_indices: np.ndarray
    delta_indices: np.ndarray
    delta_normalized: np.ndarray
    quality: ScatGbarQualityReport

    def summary(self) -> dict[str, Any]:
        return {
            "schema": "scat-gbar-v35-input/v2",
            "batch": self.batch,
            "external_id": self.external_id,
            "features": list(self.feature_names),
            "observed_points": len(self.timestamps),
            "absolute_binary_shape": list(self.absolute_binary.shape),
            "delta_normalized_shape": list(self.delta_normalized.shape),
            "start_time": self.timestamps[0].isoformat(),
            "end_time": self.timestamps[-1].isoformat(),
            "quality": self.quality.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class _PhysicalPoint:
    source_index: int
    timestamp: datetime
    values: tuple[float, ...]


class ScatGbarV35DataProcessor:
    """Repair a continuous trajectory first, then select its latest window."""

    observation_points = 36
    source_stride = 2
    required_source_points = (observation_points - 1) * source_stride + 1
    maximum_source_gap_seconds = SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS
    maximum_repair_run = SCAT_GBAR_MAXIMUM_REPAIR_RUN
    maximum_repair_span_seconds = SCAT_GBAR_MAXIMUM_REPAIR_SPAN_SECONDS
    maximum_sampled_distance_metres = SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES
    _spike_thresholds = SCAT_GBAR_SPIKE_THRESHOLDS

    def prepare(
        self,
        points: Sequence[Mapping[str, Any]],
        *,
        batch: str,
        external_id: str | int,
    ) -> ScatGbarPreparedWindow:
        counts = self._empty_counts()
        converted: list[_PhysicalPoint] = []
        invalid_rows = 0
        for fallback_index, raw_point in enumerate(points):
            try:
                converted.append(self._convert_point(raw_point, fallback_index, counts))
            except ScatGbarDataError:
                invalid_rows += 1
        converted.sort(key=lambda item: (item.timestamp, item.source_index))

        unique: list[_PhysicalPoint] = []
        duplicates = 0
        for point in converted:
            if unique and point.timestamp == unique[-1].timestamp:
                unique[-1] = point
                duplicates += 1
            else:
                unique.append(point)
        segments = self._split_contiguous(unique)
        repaired = self._repair_segment(segments[-1] if segments else [], counts)

        sampled: list[_PhysicalPoint] = []
        arrays: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None
        error: tuple[str, str] | None = None
        if len(repaired) < self.required_source_points:
            error = (
                "insufficient_latest_segment",
                "指定时刻之前的最新连续轨迹段不足71个源点；不会自动改用更早的轨迹段。",
            )
        else:
            sampled = repaired[-self.required_source_points :][:: self.source_stride]
            values64 = np.asarray([point.values for point in sampled], dtype=np.float64)
            if not np.all(np.isfinite(values64)):
                error = ("unrecoverable_values", "最新轨迹段仍有无法可靠修复的关键特征值。")
            elif not self._position_transitions_are_valid(values64):
                error = ("unrecoverable_position_jump", "修复后的窗口仍有无法可靠解释的位置跳变。")
            else:
                arrays = self._encode(values64)
                if not self._deltas_are_valid(arrays[2]):
                    error = ("unrecoverable_delta_jump", "修复后的窗口仍超出GBAR v35编码差分范围。")

        for column, feature in enumerate(SCAT_GBAR_FEATURES):
            counts[feature]["unrepaired"] = sum(
                not math.isfinite(point.values[column]) for point in repaired
            )
        report = self._build_report(
            points,
            unique,
            segments,
            repaired,
            sampled,
            counts,
            invalid_rows,
            duplicates,
            error is None,
        )
        if error is not None or arrays is None:
            code, message = error or ("no_valid_window", "无法构建GBAR v35模型窗口。")
            raise ScatGbarDataError(code, message, quality=report)

        values = np.asarray([point.values for point in sampled], dtype=np.float32)
        absolute_binary, indices, deltas, normalized = arrays
        for array in (values, absolute_binary, indices, deltas, normalized):
            array.setflags(write=False)
        timestamps = tuple(point.timestamp for point in sampled)
        start_time = timestamps[0]
        return ScatGbarPreparedWindow(
            batch=batch,
            external_id=str(external_id),
            feature_names=SCAT_GBAR_FEATURES,
            source_indices=tuple(point.source_index for point in sampled),
            timestamps=timestamps,
            time_offsets_seconds=tuple(
                round((timestamp - start_time).total_seconds(), 3) for timestamp in timestamps
            ),
            feature_values=values,
            absolute_binary=absolute_binary,
            interval_indices=indices,
            delta_indices=deltas,
            delta_normalized=normalized,
            quality=report,
        )

    @staticmethod
    def _empty_counts() -> dict[str, dict[str, int]]:
        return {
            feature: {
                key: 0
                for key in (
                    "missing",
                    "range",
                    "spike",
                    "interpolated",
                    "extrapolated",
                    "unrepaired",
                )
            }
            for feature in SCAT_GBAR_FEATURES
        }

    def _convert_point(
        self,
        point: Mapping[str, Any],
        fallback_index: int,
        counts: dict[str, dict[str, int]],
    ) -> _PhysicalPoint:
        timestamp = self._timestamp(point.get("timestamp"))
        index_value = point.get("source_index", fallback_index)
        if isinstance(index_value, bool):
            raise ScatGbarDataError("invalid_source_index", "SCAT轨迹点序号无效。")
        try:
            source_index = int(index_value)
        except (TypeError, ValueError) as exc:
            raise ScatGbarDataError("invalid_source_index", "SCAT轨迹点序号无效。") from exc
        raw_values = tuple(point.get(name) for name in SCAT_GBAR_FEATURES)
        multipliers = (1.0, 1.0, FLIGHT_LEVEL_TO_METRES, 1.0, 1.0, FPM_TO_MPS)
        physical: list[float] = []
        for feature, raw_value, multiplier in zip(
            SCAT_GBAR_FEATURES, raw_values, multipliers, strict=True
        ):
            value = self._optional_float(raw_value)
            if value is None:
                counts[feature]["missing"] += 1
                physical.append(math.nan)
                continue
            value *= multiplier
            encoding = SCAT_GBAR_ENCODING[feature]
            if not encoding.minimum <= value <= encoding.maximum:
                counts[feature]["range"] += 1
                physical.append(math.nan)
            else:
                physical.append(value)
        return _PhysicalPoint(source_index, timestamp, tuple(physical))

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
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ScatGbarDataError("invalid_timestamp", "SCAT时间戳格式无效。") from exc
        else:
            raise ScatGbarDataError("invalid_timestamp", "SCAT轨迹点缺少时间戳。")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _split_contiguous(self, points: Sequence[_PhysicalPoint]) -> list[list[_PhysicalPoint]]:
        segments: list[list[_PhysicalPoint]] = []
        current: list[_PhysicalPoint] = []
        for point in points:
            if current:
                gap = (point.timestamp - current[-1].timestamp).total_seconds()
                if gap <= 0 or gap > self.maximum_source_gap_seconds:
                    segments.append(current)
                    current = []
            current.append(point)
        if current:
            segments.append(current)
        return segments

    def _repair_segment(
        self,
        segment: Sequence[_PhysicalPoint],
        counts: dict[str, dict[str, int]],
    ) -> list[_PhysicalPoint]:
        if not segment:
            return []
        values = np.asarray([point.values for point in segment], dtype=np.float64)
        times = np.asarray(
            [(point.timestamp - segment[0].timestamp).total_seconds() for point in segment]
        )
        self._mark_position_spikes(values, counts)
        for column, feature in enumerate(SCAT_GBAR_FEATURES[2:], start=2):
            self._mark_scalar_spikes(values[:, column], feature, counts)
        for column, feature in enumerate(SCAT_GBAR_FEATURES):
            self._repair_feature(values[:, column], times, feature, counts)
        return [
            _PhysicalPoint(point.source_index, point.timestamp, tuple(values[row]))
            for row, point in enumerate(segment)
        ]

    def _mark_position_spikes(self, values: np.ndarray, counts: dict[str, dict[str, int]]) -> None:
        marked: set[int] = set()
        for index in range(1, len(values) - 1):
            previous, current, following = values[index - 1 : index + 2, :2]
            if not np.all(np.isfinite((previous, current, following))):
                continue
            if (
                self._haversine_metres(*previous, *current) > self.maximum_sampled_distance_metres
                and self._haversine_metres(*current, *following)
                > self.maximum_sampled_distance_metres
                and self._haversine_metres(*previous, *following)
                <= self.maximum_sampled_distance_metres
            ):
                marked.add(index)
        if len(values) >= 3 and np.all(np.isfinite(values[-3:, :2])):
            if (
                self._haversine_metres(*values[-2, :2], *values[-1, :2])
                > self.maximum_sampled_distance_metres
                and self._haversine_metres(*values[-3, :2], *values[-2, :2])
                <= self.maximum_sampled_distance_metres
            ):
                marked.add(len(values) - 1)
        if len(values) >= 3 and np.all(np.isfinite(values[-3:, :2])):
            if (
                self._haversine_metres(*values[-2, :2], *values[-1, :2])
                > self.maximum_sampled_distance_metres
                and self._haversine_metres(*values[-3, :2], *values[-2, :2])
                <= self.maximum_sampled_distance_metres
            ):
                marked.add(len(values) - 1)
        for index in marked:
            values[index, :2] = np.nan
        counts["lat"]["spike"] += len(marked)
        counts["lon"]["spike"] += len(marked)

    def _mark_scalar_spikes(
        self,
        values: np.ndarray,
        feature: str,
        counts: dict[str, dict[str, int]],
    ) -> None:
        threshold = self._spike_thresholds[feature]
        original = values.copy()
        marked: set[int] = set()
        for index in range(1, len(original) - 1):
            previous, current, following = original[index - 1 : index + 2]
            if np.all(np.isfinite((previous, current, following))) and (
                abs(current - (previous + following) / 2.0) > threshold
                and abs(following - previous) <= threshold
            ):
                marked.add(index)
        if (
            len(original) >= 3
            and np.all(np.isfinite(original[-3:]))
            and (
                abs(original[-1] - original[-2]) > threshold
                and abs(original[-2] - original[-3]) <= threshold
            )
        ):
            marked.add(len(original) - 1)
        for index in marked:
            values[index] = np.nan
        counts[feature]["spike"] += len(marked)

    def _repair_feature(
        self,
        values: np.ndarray,
        times: np.ndarray,
        feature: str,
        counts: dict[str, dict[str, int]],
    ) -> None:
        for start, end in self._missing_runs(~np.isfinite(values)):
            length = end - start
            if length > self.maximum_repair_run:
                continue
            left, right = start - 1, end
            if left >= 0 and right < len(values):
                if times[right] - times[left] <= self.maximum_repair_span_seconds:
                    values[start:end] = np.interp(
                        times[start:end], (times[left], times[right]), (values[left], values[right])
                    )
                    counts[feature]["interpolated"] += length
            elif start == 0 and right + 1 < len(values):
                self._linear_extrapolate(values, times, start, end, right, right + 1)
                counts[feature]["extrapolated"] += length
            elif end == len(values) and left - 1 >= 0:
                self._linear_extrapolate(values, times, start, end, left - 1, left)
                counts[feature]["extrapolated"] += length
        encoding = SCAT_GBAR_ENCODING[feature]
        finite = np.isfinite(values)
        values[finite] = np.clip(values[finite], encoding.minimum, encoding.maximum)

    @staticmethod
    def _missing_runs(mask: np.ndarray) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for index, missing in enumerate(mask):
            if missing and start is None:
                start = index
            elif not missing and start is not None:
                runs.append((start, index))
                start = None
        if start is not None:
            runs.append((start, len(mask)))
        return runs

    @staticmethod
    def _linear_extrapolate(
        values: np.ndarray,
        times: np.ndarray,
        start: int,
        end: int,
        anchor_a: int,
        anchor_b: int,
    ) -> None:
        elapsed = times[anchor_b] - times[anchor_a]
        if elapsed > 0:
            slope = (values[anchor_b] - values[anchor_a]) / elapsed
            values[start:end] = values[anchor_a] + slope * (times[start:end] - times[anchor_a])

    def _build_report(
        self,
        points: Sequence[Mapping[str, Any]],
        unique: Sequence[_PhysicalPoint],
        segments: Sequence[Sequence[_PhysicalPoint]],
        repaired: Sequence[_PhysicalPoint],
        sampled: Sequence[_PhysicalPoint],
        counts: dict[str, dict[str, int]],
        invalid_rows: int,
        duplicates: int,
        model_ready: bool,
    ) -> ScatGbarQualityReport:
        details = tuple(
            ScatGbarFeatureQuality(
                feature,
                counts[feature]["missing"],
                counts[feature]["range"],
                counts[feature]["spike"],
                counts[feature]["interpolated"],
                counts[feature]["extrapolated"],
                counts[feature]["unrepaired"],
            )
            for feature in SCAT_GBAR_FEATURES
        )
        return ScatGbarQualityReport(
            len(points),
            invalid_rows,
            duplicates,
            len(segments),
            max(0, len(segments) - 1),
            len(repaired),
            self._median_period(unique),
            self._median_period(sampled),
            model_ready,
            details,
        )

    @staticmethod
    def _haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6_371_000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        delta_phi, delta_lambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = (
            math.sin(delta_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        )
        return 2 * radius * math.asin(min(1.0, math.sqrt(a)))

    def _position_transitions_are_valid(self, values: np.ndarray) -> bool:
        return all(
            self._haversine_metres(*previous[:2], *current[:2])
            <= self.maximum_sampled_distance_metres
            for previous, current in zip(values[:-1], values[1:], strict=True)
        )

    @staticmethod
    def _encode(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        indices = np.empty(values.shape, dtype=np.int32)
        binary: list[np.ndarray] = []
        for column, feature in enumerate(SCAT_GBAR_FEATURES):
            config = SCAT_GBAR_ENCODING[feature]
            levels = (1 << config.bits) - 1
            scaled = (
                (values[:, column] - config.minimum) / (config.maximum - config.minimum) * levels
            )
            encoded = np.clip(np.rint(scaled).astype(np.int32), 0, levels)
            indices[:, column] = encoded
            shifts = np.arange(config.bits - 1, -1, -1, dtype=np.int32)
            binary.append(((encoded[:, None] >> shifts) & 1).astype(np.uint8))
        deltas = indices[1:] - indices[:-1]
        minimum = np.asarray(
            [SCAT_GBAR_ENCODING[name].delta_minimum for name in SCAT_GBAR_FEATURES],
            dtype=np.float32,
        )
        maximum = np.asarray(
            [SCAT_GBAR_ENCODING[name].delta_maximum for name in SCAT_GBAR_FEATURES],
            dtype=np.float32,
        )
        normalized = (deltas.astype(np.float32) - minimum) / (maximum - minimum)
        return np.concatenate(binary, axis=1), indices, deltas, normalized.astype(np.float32)

    @staticmethod
    def _deltas_are_valid(deltas: np.ndarray) -> bool:
        return all(
            not (
                np.any(deltas[:, column] < SCAT_GBAR_ENCODING[feature].delta_minimum)
                or np.any(deltas[:, column] > SCAT_GBAR_ENCODING[feature].delta_maximum)
            )
            for column, feature in enumerate(SCAT_GBAR_FEATURES)
        )

    @staticmethod
    def _median_period(points: Sequence[_PhysicalPoint]) -> float:
        periods = [
            (current.timestamp - previous.timestamp).total_seconds()
            for previous, current in zip(points[:-1], points[1:], strict=True)
            if current.timestamp > previous.timestamp
        ]
        return round(float(np.median(periods)), 3) if periods else 0.0


__all__ = [
    "FEET_TO_METRES",
    "FPM_TO_MPS",
    "FLIGHT_LEVEL_TO_METRES",
    "SCAT_GBAR_FEATURES",
    "SCAT_GBAR_QUALITY_POLICY_VERSION",
    "SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS",
    "SCAT_GBAR_MAXIMUM_REPAIR_RUN",
    "SCAT_GBAR_MAXIMUM_REPAIR_SPAN_SECONDS",
    "SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES",
    "SCAT_GBAR_SPIKE_THRESHOLDS",
    "SCAT_GBAR_ENCODING",
    "FeatureEncoding",
    "ScatGbarDataError",
    "ScatGbarFeatureQuality",
    "ScatGbarQualityReport",
    "ScatGbarPreparedWindow",
    "ScatGbarV35DataProcessor",
]
