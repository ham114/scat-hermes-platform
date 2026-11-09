"""M6 自研预测服务：单航班即时轨迹预测（对话工具与后续实验的共用入口）。

口径与边界：

- 输入是**实飞观测**（到 `as_of`，缺省为轨迹末点），输出是自研预测器对目标时刻的
  预测点；与「SCAT 原始预测评估」（CFPL 计划航路 vs 实飞，M5 冻结口径）以及任务
  模型（GBAR）评估是**不同体系**，数字不得混列或直接对比；
- 只读原始集合，不写任何集合（实验台账由 `scat.experiments` 负责）；
- 预测器经 `scat.prediction.registry` 解析，id 与版本随结果返回，保证可追溯；
- 结果行有界：单次调用最多 6 档时域。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from scat.prediction.alignment import (
    DEFAULT_MAX_TRACK_GAP_S,
    HORIZONS_S,
    TrackPoint,
    build_track,
    haversine_metres,
    interpolate_track,
)
from scat.prediction.defaults import DEFAULT_PREDICTOR_ID
from scat.prediction.interfaces import (
    PREDICTOR_SCHEMA_VERSION,
    PredictionRequest,
    TrajectoryPredictor,
)
from scat.prediction.registry import PredictorRegistry, default_registry
from scat.repositories.flight_repository import ScatFlightRepository

MAX_HORIZONS = 6
MAX_HORIZON_S = 3600.0
MIN_OBSERVED_POINTS = 3


class PredictionService:
    """单航班即时预测：实飞观测 → 预测点 →（可选）实飞真值对照。"""

    def __init__(
        self,
        repository: ScatFlightRepository | None = None,
        *,
        registry: PredictorRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry or default_registry

    @property
    def repository(self) -> ScatFlightRepository:
        if self._repository is None:
            self._repository = ScatFlightRepository()
        return self._repository

    def resolve_predictor(self, predictor_id: str) -> TrajectoryPredictor:
        """按 id 解析预测器；未知 id 抛出 ValueError（附可用列表）。"""
        try:
            return self._registry.get(predictor_id)
        except KeyError as error:
            available = ", ".join(pid for pid, _version in self._registry.list_predictors())
            raise ValueError(f"未知预测器 {predictor_id!r}；可用: {available}") from error

    async def predict_flight(
        self,
        *,
        batch: str,
        external_id: str,
        predictor_id: str = DEFAULT_PREDICTOR_ID,
        as_of: str | datetime | None = None,
        horizons_s: Sequence[float] | None = None,
        compare_actual: bool = True,
    ) -> dict[str, Any] | None:
        """返回单航班即时预测结果；航班不存在或无有效轨迹时返回 None。"""
        predictor = self.resolve_predictor(predictor_id)
        horizons = _normalize_horizons(horizons_s)
        inputs = await self.repository.get_evaluation_inputs(
            batch=batch,
            external_id=external_id,
        )
        if not inputs:
            return None
        track, _duplicates = build_track(inputs.get("plots") or [])
        if len(track) < MIN_OBSERVED_POINTS:
            return None
        cutoff = _parse_cutoff(as_of, track)
        observed = [point for point in track if point.time <= cutoff]
        if len(observed) < MIN_OBSERVED_POINTS:
            raise ValueError(
                f"as_of 之前的实飞观测点不足 {MIN_OBSERVED_POINTS} 个，无法预测（可检查时间或改小 as_of）。"
            )
        targets = tuple(cutoff + timedelta(seconds=horizon) for horizon in horizons)
        try:
            result = predictor.predict(
                PredictionRequest(observed=tuple(observed), target_times=targets)
            )
        except ValueError as error:
            raise ValueError(f"预测器拒绝该请求：{error}") from error

        rows: list[dict[str, Any]] = []
        for horizon, target, fix in zip(horizons, targets, result.fixes):
            actual_point: TrackPoint | None = None
            exclusion_reason: str | None = None
            error_m: float | None = None
            if compare_actual:
                actual_point, reason = interpolate_track(
                    track,
                    target,
                    max_gap_s=DEFAULT_MAX_TRACK_GAP_S,
                )
                exclusion_reason = reason.value if reason is not None else None
                if actual_point is not None:
                    error_m = haversine_metres(
                        fix.lat,
                        fix.lon,
                        actual_point.lat,
                        actual_point.lon,
                    )
            rows.append(
                {
                    "horizon_s": horizon,
                    "target_time": _iso(target),
                    "predicted": {
                        "lat": round(fix.lat, 6),
                        "lon": round(fix.lon, 6),
                        "altitude_m": fix.altitude_m,
                    },
                    "actual": None
                    if actual_point is None
                    else {
                        "lat": round(actual_point.lat, 6),
                        "lon": round(actual_point.lon, 6),
                        "altitude_m": actual_point.altitude_m,
                    },
                    "error_m": None if error_m is None else round(error_m, 1),
                    "actual_exclusion_reason": exclusion_reason,
                }
            )
        return {
            "batch": batch,
            "external_id": external_id,
            "predictor": {
                "id": predictor.predictor_id,
                "version": predictor.predictor_version,
                "schema_version": PREDICTOR_SCHEMA_VERSION,
            },
            "cutoff_time": _iso(cutoff),
            "observed": {
                "point_count": len(observed),
                "start_time": _iso(observed[0].time),
                "end_time": _iso(observed[-1].time),
            },
            "horizons_s": list(horizons),
            "rows": rows,
            "actual_compared": compare_actual,
            "note": (
                "即时预测＝自研模型基于实飞观测外推；与 SCAT 原始预测评估（CFPL 计划 vs 实飞，"
                "scat-prediction-metrics-v2）及任务模型（GBAR）评估是不同体系，数字不得混列。"
            ),
        }


def _normalize_horizons(values: Sequence[float] | None) -> tuple[float, ...]:
    if values is None:
        return tuple(float(value) for value in HORIZONS_S)
    cleaned = tuple(sorted({float(value) for value in values}))
    if not cleaned:
        raise ValueError("horizons_s 不能为空。")
    if len(cleaned) > MAX_HORIZONS:
        raise ValueError(f"最多支持 {MAX_HORIZONS} 档时域。")
    if any(value <= 0 or value > MAX_HORIZON_S for value in cleaned):
        raise ValueError(f"时域必须在 (0, {MAX_HORIZON_S:.0f}] 秒范围内。")
    return cleaned


def _parse_cutoff(as_of: str | datetime | None, track: list[TrackPoint]) -> datetime:
    if as_of is None:
        return track[-1].time
    if isinstance(as_of, datetime):
        parsed = as_of
    else:
        normalized = str(as_of).strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError("as_of 必须是有效的 ISO 8601 时间") from error
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


__all__ = [
    "DEFAULT_PREDICTOR_ID",
    "MAX_HORIZONS",
    "MAX_HORIZON_S",
    "MIN_OBSERVED_POINTS",
    "PredictionService",
]
