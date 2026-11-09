"""SCAT 航班业务服务。

该模块位于 Repository 与 Agent Tool 之间：

Agent Tool
    -> ScatFlightService
    -> ScatFlightRepository
    -> MongoDB

Repository 负责数据库查询；
Service 负责业务整理、单位转换和结果标准化。
"""

from __future__ import annotations

import asyncio
import bisect
import math
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from core.settings import settings
from scat.materialization.payloads import data_quality_payload, statistics_payload
from scat.preprocessing.gbar_v35 import SCAT_GBAR_QUALITY_POLICY_VERSION
from scat.quality_cache import RedisQualitySnapshotCache, redis_quality_snapshot_cache
from scat.repositories.flight_repository import ScatFlightRepository

if TYPE_CHECKING:
    from scat.services.materialization_service import MaterializationService

FEET_TO_METRES = 0.3048
MPS_TO_KNOTS = 1.9438444924406
FPM_TO_MPS = FEET_TO_METRES / 60.0

# 预测误差统计使用的航迹点匹配窗口与地球半径。
EARTH_RADIUS_METRES = 6_371_000.0
QUALITY_SNAPSHOT_TTL_SECONDS = settings.SCAT_QUALITY_SNAPSHOT_TTL_SECONDS
QUALITY_PROBLEM_SORT_FIELDS = frozenset(
    {
        "outlier_point_count",
        "missing_point_count",
        "spike_value_count",
        "point_count",
        "external_id",
    }
)
QUALITY_PROBLEM_TYPES = frozenset({"any", "missing", "outlier", "spike"})


def _haversine_metres(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """两个经纬度之间的球面距离（米）。"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METRES * math.asin(math.sqrt(a))


class ScatFlightService:
    """SCAT 航班业务服务。"""

    def __init__(
        self,
        repository: ScatFlightRepository | None = None,
        *,
        materialization_service: MaterializationService | None = None,
        quality_snapshot_cache: RedisQualitySnapshotCache | None = None,
    ) -> None:
        self.repository = repository or ScatFlightRepository()
        # M4-09：注入后总览优先读物化统计，避免每次扫描完整原始集合。
        self.materialization_service = materialization_service
        self._quality_snapshot_cache = quality_snapshot_cache or redis_quality_snapshot_cache
        self._quality_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._quality_query_lock = asyncio.Lock()

    async def sample_gbar_model_flights(
        self,
        *,
        batch: str,
        sample_size: int,
        observation_points: int,
        callsign: str | None = None,
        callsigns: tuple[str, ...] = (),
        exclude_callsigns: tuple[str, ...] = (),
        callsign_prefix: str | None = None,
        aircraft_type: str | None = None,
        aircraft_types: tuple[str, ...] = (),
        exclude_aircraft_types: tuple[str, ...] = (),
        adep: str | None = None,
        adeps: tuple[str, ...] = (),
        exclude_adeps: tuple[str, ...] = (),
        ades: str | None = None,
        adess: tuple[str, ...] = (),
        exclude_adess: tuple[str, ...] = (),
        external_id: str | None = None,
        external_ids: tuple[str, ...] = (),
        exclude_external_ids: tuple[str, ...] = (),
        candidate_key: str | None = None,
        takeoff_start: datetime | None = None,
        takeoff_end: datetime | None = None,
        min_track_points: int | None = None,
        max_track_points: int | None = None,
        has_fpl: bool | None = None,
        has_scat_prediction: bool | None = None,
        end_time: datetime | None = None,
        future_points: int = 100,
        _maximum_sample_size: int = 16,
    ) -> dict[str, Any]:
        """Select random trajectories that can support a complete GBAR backtest."""
        normalized_batch = batch.strip()
        if not normalized_batch:
            raise ValueError("batch 不能为空")
        if not 1 <= _maximum_sample_size <= 50:
            raise ValueError("随机轨迹数量上限必须在 1 到 50 之间")
        if not 1 <= sample_size <= _maximum_sample_size:
            raise ValueError(f"随机轨迹数量必须在 1 到 {_maximum_sample_size} 之间")
        if not 71 <= observation_points <= 2_000:
            raise ValueError("GBAR 每条轨迹的源点数必须在 71 到 2000 之间")
        if end_time is not None and (end_time.tzinfo is None or end_time.utcoffset() is None):
            raise ValueError("历史截止时间必须包含时区")
        normalized_takeoff_start = self._normalize_optional_datetime(takeoff_start, "takeoff_start")
        normalized_takeoff_end = self._normalize_optional_datetime(takeoff_end, "takeoff_end")
        if (
            normalized_takeoff_start is not None
            and normalized_takeoff_end is not None
            and normalized_takeoff_start >= normalized_takeoff_end
        ):
            raise ValueError("takeoff_start 必须早于 takeoff_end")
        normalized_filters = {
            "callsign": self._optional_code(callsign),
            "callsigns": self._optional_codes(callsigns),
            "exclude_callsigns": self._optional_codes(exclude_callsigns),
            "callsign_prefix": self._optional_code(callsign_prefix),
            "aircraft_type": self._optional_code(aircraft_type),
            "aircraft_types": self._optional_codes(aircraft_types),
            "exclude_aircraft_types": self._optional_codes(exclude_aircraft_types),
            "adep": self._optional_code(adep),
            "adeps": self._optional_codes(adeps),
            "exclude_adeps": self._optional_codes(exclude_adeps),
            "ades": self._optional_code(ades),
            "adess": self._optional_codes(adess),
            "exclude_adess": self._optional_codes(exclude_adess),
            "external_id": self._optional_text(external_id),
            "external_ids": self._optional_texts(external_ids),
            "exclude_external_ids": self._optional_texts(exclude_external_ids),
            "candidate_key": self._optional_text(candidate_key),
            "min_track_points": min_track_points,
            "max_track_points": max_track_points,
            "has_fpl": has_fpl,
            "has_scat_prediction": has_scat_prediction,
        }
        source_end_time = (
            end_time.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds")
            if end_time is not None
            else None
        )
        selection = await self.repository.sample_gbar_flight_ids(
            batch=normalized_batch,
            sample_size=sample_size,
            observation_points=observation_points,
            maximum_sample_size=_maximum_sample_size,
            **normalized_filters,
            takeoff_start=normalized_takeoff_start,
            takeoff_end=normalized_takeoff_end,
            end_time=source_end_time,
            future_points=future_points,
        )
        external_ids = selection["external_ids"]
        return {
            "status": "ok" if len(external_ids) == sample_size else "not_enough_candidates",
            "operation": "sample_gbar_model_flights",
            "query": {
                "batch": normalized_batch,
                "sample_size": sample_size,
                "observation_points": observation_points,
                "forecast_cutoff": end_time.isoformat() if end_time is not None else None,
                "future_points": future_points,
                **normalized_filters,
                "takeoff_start": (
                    normalized_takeoff_start.isoformat()
                    if normalized_takeoff_start is not None
                    else None
                ),
                "takeoff_end": (
                    normalized_takeoff_end.isoformat()
                    if normalized_takeoff_end is not None
                    else None
                ),
            },
            "data": {
                "requested_count": sample_size,
                "selected_count": len(external_ids),
                "eligible_count": selection["eligible_count"],
                "external_ids": external_ids,
            },
        }

    async def sample_comparison_model_flights(
        self,
        *,
        batch: str,
        sample_size: int,
        callsign: str | None = None,
        callsigns: tuple[str, ...] = (),
        exclude_callsigns: tuple[str, ...] = (),
        callsign_prefix: str | None = None,
        aircraft_type: str | None = None,
        aircraft_types: tuple[str, ...] = (),
        exclude_aircraft_types: tuple[str, ...] = (),
        adep: str | None = None,
        adeps: tuple[str, ...] = (),
        exclude_adeps: tuple[str, ...] = (),
        ades: str | None = None,
        adess: tuple[str, ...] = (),
        exclude_adess: tuple[str, ...] = (),
        external_id: str | None = None,
        external_ids: tuple[str, ...] = (),
        exclude_external_ids: tuple[str, ...] = (),
        candidate_key: str | None = None,
        takeoff_start: datetime | None = None,
        takeoff_end: datetime | None = None,
        min_track_points: int | None = None,
        max_track_points: int | None = None,
    ) -> dict[str, Any]:
        """Randomly select flights that meet the coarse comparison prerequisites.

        The comparison resolver remains the final authority for per-cutoff CFPL and
        truth coverage. This selector guarantees enough GBAR observation/holdout
        points plus the presence of FPL and SCAT prediction snapshots, then freezes
        the random IDs before confirmation.
        """
        required_track_points = 71 + 100
        if max_track_points is not None and max_track_points < required_track_points:
            raise ValueError(
                f"统一预测对比要求至少 {required_track_points} 个轨迹点，"
                "max_track_points 不能低于该值"
            )
        effective_min_track_points = max(min_track_points or 0, required_track_points)
        result = await self.sample_gbar_model_flights(
            batch=batch,
            sample_size=sample_size,
            observation_points=71,
            callsign=callsign,
            callsigns=callsigns,
            exclude_callsigns=exclude_callsigns,
            callsign_prefix=callsign_prefix,
            aircraft_type=aircraft_type,
            aircraft_types=aircraft_types,
            exclude_aircraft_types=exclude_aircraft_types,
            adep=adep,
            adeps=adeps,
            exclude_adeps=exclude_adeps,
            ades=ades,
            adess=adess,
            exclude_adess=exclude_adess,
            external_id=external_id,
            external_ids=external_ids,
            exclude_external_ids=exclude_external_ids,
            candidate_key=candidate_key,
            takeoff_start=takeoff_start,
            takeoff_end=takeoff_end,
            min_track_points=effective_min_track_points,
            max_track_points=max_track_points,
            has_fpl=True,
            has_scat_prediction=True,
            future_points=100,
            _maximum_sample_size=50,
        )
        result["operation"] = "sample_comparison_model_flights"
        query = result.get("query")
        if isinstance(query, dict):
            query["eligibility_profile"] = "gbar-align-v1-random-selection"
        return result

    async def _cached_quality_query(
        self,
        key: tuple[Any, ...],
        loader: Callable[[], Awaitable[dict[str, Any]]],
        *,
        ttl_seconds: float = QUALITY_SNAPSHOT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Coalesce concurrent quality scans and briefly reuse immutable results."""
        async with self._quality_query_lock:
            now = time.monotonic()
            cached = self._quality_cache.get(key)
            if cached is not None and cached[0] > now:
                return deepcopy(cached[1])
            result = await loader()
            completed_at = time.monotonic()
            self._quality_cache = {
                cache_key: value
                for cache_key, value in self._quality_cache.items()
                if value[0] > completed_at
            }
            self._quality_cache[key] = (
                completed_at + ttl_seconds,
                deepcopy(result),
            )
            return result

    async def _get_quality_snapshot(self, batch: str | None) -> dict[str, Any]:
        """Load one compact per-flight snapshot shared by all quality views."""
        if self._quality_snapshot_cache.enabled:
            cached = await self._quality_snapshot_cache.get(
                batch=batch,
                policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
            )
            if cached is not None:
                return deepcopy(cached)
            async with self._quality_query_lock:
                cached = await self._quality_snapshot_cache.get(
                    batch=batch,
                    policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
                )
                if cached is not None:
                    return deepcopy(cached)
                result = await self._load_quality_snapshot(batch)
                await self._quality_snapshot_cache.put(
                    result,
                    batch=batch,
                    policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
                )
                return result
        return await self._cached_quality_query(
            (SCAT_GBAR_QUALITY_POLICY_VERSION, "shared_snapshot", batch),
            lambda: self._load_quality_snapshot(batch),
        )

    async def _load_quality_snapshot(self, batch: str | None) -> dict[str, Any]:
        return await self.repository.get_model_data_quality_summary(
            batch=batch,
            include_flight_rows=True,
        )

    async def rebuild_quality_snapshot(self, *, batch: str | None = None) -> dict[str, Any]:
        """Explicitly replace one shared quality snapshot after a policy/data refresh."""
        normalized_batch = self._optional_text(batch)
        async with self._quality_query_lock:
            if self._quality_snapshot_cache.enabled:
                await self._quality_snapshot_cache.delete(
                    batch=normalized_batch,
                    policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
                )
            result = await self._load_quality_snapshot(normalized_batch)
            if self._quality_snapshot_cache.enabled:
                stored = await self._quality_snapshot_cache.put(
                    result,
                    batch=normalized_batch,
                    policy_version=SCAT_GBAR_QUALITY_POLICY_VERSION,
                )
                if not stored:
                    raise RuntimeError("shared quality snapshot cache write failed")
            else:
                completed_at = time.monotonic()
                self._quality_cache[
                    (SCAT_GBAR_QUALITY_POLICY_VERSION, "shared_snapshot", normalized_batch)
                ] = (
                    completed_at + QUALITY_SNAPSHOT_TTL_SECONDS,
                    deepcopy(result),
                )
            return deepcopy(result)

    @staticmethod
    def _quality_row_has_problem(row: dict[str, Any]) -> bool:
        return (
            any(
                int(row.get(field) or 0) > 0
                for field in (
                    "missing_point_count",
                    "outlier_point_count",
                    "spike_value_count",
                )
            )
            or int(row.get("point_count") or 0) == 0
        )

    @classmethod
    def _quality_row_is_clean(cls, row: dict[str, Any]) -> bool:
        return int(row.get("point_count") or 0) > 0 and not cls._quality_row_has_problem(row)

    @classmethod
    def _quality_row_matches_problem_type(
        cls,
        row: dict[str, Any],
        problem_type: str,
    ) -> bool:
        if problem_type == "any":
            return cls._quality_row_has_problem(row)
        field = {
            "missing": "missing_point_count",
            "outlier": "outlier_point_count",
            "spike": "spike_value_count",
        }[problem_type]
        return int(row.get(field) or 0) > 0

    @staticmethod
    def _quality_page(limit: int, offset: int) -> tuple[int, int]:
        safe_limit = 20 if limit <= 0 else min(limit, 100)
        return safe_limit, max(offset, 0)

    # =========================================================
    # 公共查询方法
    # =========================================================

    async def get_database_overview(self) -> dict[str, Any]:
        """返回 SCAT 航班数据库的基本概况。"""
        total_count, batches = await asyncio.gather(
            self.repository.count_all(),
            self.repository.list_batches(),
        )

        return {
            "status": "ok",
            "data": {
                "total_flights": total_count,
                "batch_count": len(batches),
                "batches": self._json_safe(batches),
            },
        }

    async def list_batches(
        self,
        *,
        limit: int = 50,
        skip: int = 0,
    ) -> dict[str, Any]:
        """列出可用的 SCAT 数据批次。"""
        total, batches = await asyncio.gather(
            self.repository.count_batches(),
            self.repository.list_batches(limit=limit, skip=skip),
        )
        safe_skip = max(skip, 0)

        return {
            "status": "ok",
            "query": {"limit": limit, "skip": safe_skip},
            "data": {
                "count": len(batches),
                "total": total,
                "has_more": safe_skip + len(batches) < total,
                "items": self._json_safe(batches),
            },
        }

    async def get_model_data_quality_summary(
        self,
        *,
        batch: str | None = None,
    ) -> dict[str, Any]:
        """Return one server-side full-collection GBAR quality aggregation."""
        normalized_batch = self._optional_text(batch)
        data = await self._get_quality_snapshot(normalized_batch)
        data.pop("_quality_flight_rows", None)
        return {
            "status": "ok",
            "query": {"batch": normalized_batch},
            "data": self._json_safe(data),
        }

    async def list_model_data_quality_problem_flights(
        self,
        *,
        batch: str | None = None,
        limit: int = 20,
        offset: int = 0,
        problem_type: Literal["any", "missing", "outlier", "spike"] = "any",
        sort_by: str = "outlier_point_count",
    ) -> dict[str, Any]:
        """分页返回包含缺失、越界或跳变点的问题航班列表。"""
        normalized_batch = self._optional_text(batch)
        if problem_type not in QUALITY_PROBLEM_TYPES:
            raise ValueError("problem_type 必须是 " + ", ".join(sorted(QUALITY_PROBLEM_TYPES)))
        if sort_by not in QUALITY_PROBLEM_SORT_FIELDS:
            raise ValueError("sort_by 必须是 " + ", ".join(sorted(QUALITY_PROBLEM_SORT_FIELDS)))
        safe_limit, safe_offset = self._quality_page(limit, offset)
        snapshot = await self._get_quality_snapshot(normalized_batch)
        rows = [
            row
            for row in snapshot.pop("_quality_flight_rows", [])
            if self._quality_row_matches_problem_type(row, problem_type)
        ]
        if sort_by == "external_id":
            rows.sort(
                key=lambda row: (
                    str(row.get("batch") or ""),
                    str(row.get("external_id") or ""),
                )
            )
        else:
            rows.sort(
                key=lambda row: (
                    -(int(row.get(sort_by) or 0)),
                    str(row.get("batch") or ""),
                    str(row.get("external_id") or ""),
                )
            )
        items = rows[safe_offset : safe_offset + safe_limit]
        data = {
            "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
            "trajectory_ordering": snapshot.get("trajectory_ordering"),
            "total": len(rows),
            "offset": safe_offset,
            "limit": safe_limit,
            "returned": len(items),
            "has_more": safe_offset + len(items) < len(rows),
            "items": items,
            "cache_scope": "shared_quality_snapshot",
            "cache_ttl_seconds": int(QUALITY_SNAPSHOT_TTL_SECONDS),
        }
        return {
            "status": "ok",
            "query": {
                "batch": normalized_batch,
                "limit": limit,
                "offset": offset,
                "problem_type": problem_type,
                "sort_by": sort_by,
            },
            "data": self._json_safe(data),
        }

    async def list_model_data_quality_clean_flights(
        self,
        *,
        batch: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页返回当前质量策略下无缺失、越界和跳变的SCAT轨迹。"""
        normalized_batch = self._optional_text(batch)
        safe_limit, safe_offset = self._quality_page(limit, offset)
        snapshot = await self._get_quality_snapshot(normalized_batch)
        rows = [
            row
            for row in snapshot.pop("_quality_flight_rows", [])
            if self._quality_row_is_clean(row)
        ]
        rows.sort(
            key=lambda row: (
                str(row.get("batch") or ""),
                str(row.get("external_id") or ""),
            )
        )
        items = rows[safe_offset : safe_offset + safe_limit]
        data = {
            "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
            "trajectory_ordering": snapshot.get("trajectory_ordering"),
            "total": len(rows),
            "offset": safe_offset,
            "limit": safe_limit,
            "returned": len(items),
            "has_more": safe_offset + len(items) < len(rows),
            "items": items,
            "clean_definition": snapshot.get("clean_definition"),
            "identifier_note": snapshot.get("identifier_note"),
            "cache_scope": "shared_quality_snapshot",
            "cache_ttl_seconds": int(QUALITY_SNAPSHOT_TTL_SECONDS),
        }
        return {
            "status": "ok",
            "query": {
                "batch": normalized_batch,
                "limit": limit,
                "offset": offset,
            },
            "data": self._json_safe(data),
        }

    async def get_batch_overview(
        self,
        *,
        batch: str,
        distribution_limit: int = 10,
        time_granularity: Literal["day", "hour"] = "day",
        statistics_source: Literal["auto", "live"] = "auto",
    ) -> dict[str, Any]:
        """返回一个数据批次的总体统计和主要分布。

        M4-09：`statistics_source=auto` 且存在有效物化结果时，统计与字段覆盖直接读
        `scat_batch_statistics`，不再扫描完整原始集合；分布与时间分布仍实时计算。
        """
        normalized_batch = self._require_text(
            batch,
            field_name="batch",
        )

        materialized: dict[str, Any] | None = None
        if statistics_source == "auto" and self.materialization_service is not None:
            materialized = await self.materialization_service.get_batch_statistics(
                batch=normalized_batch,
            )

        (
            aircraft_types,
            routes,
            airports,
            flight_volume,
        ) = await asyncio.gather(
            self.repository.get_aircraft_type_distribution(
                batch=normalized_batch,
                limit=distribution_limit,
            ),
            self.repository.get_route_distribution(
                batch=normalized_batch,
                limit=distribution_limit,
            ),
            self.repository.get_airport_distributions(
                batch=normalized_batch,
                limit=distribution_limit,
            ),
            self.repository.get_flight_volume(
                batch=normalized_batch,
                granularity=time_granularity,
            ),
        )

        statistics_generated_at: Any = None
        if materialized is not None:
            statistics: dict[str, Any] | None = statistics_payload(materialized)
            data_quality: dict[str, Any] | None = data_quality_payload(materialized)
            statistics_generated_at = materialized.get("generated_at")
            resolved_source = "materialized"
        else:
            statistics = await self.repository.get_batch_statistics(
                batch=normalized_batch,
            )
            data_quality = await self.repository.get_batch_data_quality(
                batch=normalized_batch,
            )
            resolved_source = "live"

        if statistics is None:
            return self._not_found(
                message=f"没有找到批次 {normalized_batch}",
                query={"batch": normalized_batch},
            )

        return {
            "status": "ok",
            "query": {
                "batch": normalized_batch,
            },
            "data": {
                "statistics": self._json_safe(statistics),
                "top_aircraft_types": self._json_safe(aircraft_types),
                "top_routes": self._json_safe(routes),
                "top_departure_airports": self._json_safe(airports["departures"]),
                "top_arrival_airports": self._json_safe(airports["arrivals"]),
                "flight_volume": self._json_safe(flight_volume),
                "data_quality": self._json_safe(data_quality),
                "statistics_source": resolved_source,
                "statistics_generated_at": self._json_safe(statistics_generated_at),
            },
        }

    async def search_flights(
        self,
        *,
        batch: str | None = None,
        callsign: str | None = None,
        callsigns: tuple[str, ...] = (),
        exclude_callsigns: tuple[str, ...] = (),
        callsign_prefix: str | None = None,
        aircraft_type: str | None = None,
        aircraft_types: tuple[str, ...] = (),
        exclude_aircraft_types: tuple[str, ...] = (),
        adep: str | None = None,
        adeps: tuple[str, ...] = (),
        exclude_adeps: tuple[str, ...] = (),
        ades: str | None = None,
        adess: tuple[str, ...] = (),
        exclude_adess: tuple[str, ...] = (),
        external_id: str | None = None,
        external_ids: tuple[str, ...] = (),
        exclude_external_ids: tuple[str, ...] = (),
        candidate_key: str | None = None,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        min_track_points: int | None = None,
        max_track_points: int | None = None,
        has_fpl: bool | None = None,
        has_scat_prediction: bool | None = None,
        sort_by: Literal["iobt", "callsign", "external_id"] = "iobt",
        sort_order: Literal["asc", "desc"] = "asc",
        limit: int = 20,
        skip: int = 0,
    ) -> dict[str, Any]:
        """按照结构化条件查询航班摘要。"""
        normalized_start_time = self._normalize_optional_datetime(start_time, "start_time")
        normalized_end_time = self._normalize_optional_datetime(end_time, "end_time")
        if (
            normalized_start_time is not None
            and normalized_end_time is not None
            and normalized_start_time >= normalized_end_time
        ):
            raise ValueError("start_time 必须早于 end_time")
        normalized_filters = {
            "batch": self._optional_text(batch),
            "callsign": self._optional_code(callsign),
            "callsigns": self._optional_codes(callsigns),
            "exclude_callsigns": self._optional_codes(exclude_callsigns),
            "callsign_prefix": self._optional_code(callsign_prefix),
            "aircraft_type": self._optional_code(aircraft_type),
            "aircraft_types": self._optional_codes(aircraft_types),
            "exclude_aircraft_types": self._optional_codes(exclude_aircraft_types),
            "adep": self._optional_code(adep),
            "adeps": self._optional_codes(adeps),
            "exclude_adeps": self._optional_codes(exclude_adeps),
            "ades": self._optional_code(ades),
            "adess": self._optional_codes(adess),
            "exclude_adess": self._optional_codes(exclude_adess),
            "external_id": self._optional_text(external_id),
            "external_ids": self._optional_texts(external_ids),
            "exclude_external_ids": self._optional_texts(exclude_external_ids),
            "candidate_key": self._optional_text(candidate_key),
            "min_track_points": min_track_points,
            "max_track_points": max_track_points,
            "has_fpl": has_fpl,
            "has_scat_prediction": has_scat_prediction,
        }
        repository_filters = {
            **normalized_filters,
            "start_time": normalized_start_time,
            "end_time": normalized_end_time,
        }
        total, records = await asyncio.gather(
            self.repository.count_flights(**repository_filters),
            self.repository.search_flights(
                **repository_filters,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                skip=skip,
            ),
        )

        items = [self._format_flight_summary(record) for record in records]

        return {
            "status": "ok",
            "query": {
                **normalized_filters,
                "start_time": self._datetime_to_utc_text(normalized_start_time),
                "end_time": self._datetime_to_utc_text(normalized_end_time),
                "sort_by": sort_by,
                "sort_order": sort_order,
                "limit": limit,
                "skip": max(skip, 0),
            },
            "data": {
                "count": len(items),
                "total": total,
                "has_more": max(skip, 0) + len(items) < total,
                "items": items,
            },
        }

    async def get_flight_overview(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any]:
        """返回单个航班的综合概况。

        同时查询：
        - 航班身份信息；
        - 轨迹统计；
        - FPL 摘要；
        - SCAT 原始预测轨迹摘要。
        """
        normalized_batch = self._require_text(
            batch,
            field_name="batch",
        )
        normalized_external_id = self._normalize_external_id(external_id)

        (
            summary,
            trajectory_summary,
            fpl_summary,
            prediction_summary,
        ) = await asyncio.gather(
            self.repository.get_summary_by_source(
                batch=normalized_batch,
                external_id=normalized_external_id,
            ),
            self.repository.get_trajectory_summary(
                batch=normalized_batch,
                external_id=normalized_external_id,
            ),
            self.repository.get_fpl_summary(
                batch=normalized_batch,
                external_id=normalized_external_id,
            ),
            self.repository.get_prediction_summary(
                batch=normalized_batch,
                external_id=normalized_external_id,
            ),
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
        }

        if summary is None:
            return self._not_found(
                message=(f"没有找到指定的 SCAT 航班：{normalized_batch}/{normalized_external_id}"),
                query=query,
            )

        return {
            "status": "ok",
            "query": query,
            "data": {
                "flight": self._format_flight_summary(summary),
                "trajectory": (self._format_trajectory_summary(trajectory_summary)),
                "flight_plan": (self._format_fpl_summary(fpl_summary)),
                "scat_prediction": (self._format_prediction_summary(prediction_summary)),
            },
        }

    async def get_track_points(
        self,
        *,
        batch: str,
        external_id: str | int,
        start: int = 0,
        limit: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
        every_nth: int = 1,
        fields: set[str] | None = None,
    ) -> dict[str, Any]:
        """返回分页后的扁平化轨迹点。

        不直接把 ASTERIX 风格的嵌套结构暴露给 Agent。
        """
        normalized_batch = self._require_text(
            batch,
            field_name="batch",
        )
        normalized_external_id = self._normalize_external_id(external_id)
        normalized_start_time = self._normalize_optional_datetime(start_time, "start_time")
        normalized_end_time = self._normalize_optional_datetime(end_time, "end_time")
        if (
            normalized_start_time is not None
            and normalized_end_time is not None
            and normalized_start_time >= normalized_end_time
        ):
            raise ValueError("start_time 必须早于 end_time")

        safe_every_nth = min(max(every_nth, 1), 20)
        allowed_fields = {"position", "motion", "mode_s", "quality"}
        selected_fields = fields or allowed_fields
        unknown_fields = selected_fields - allowed_fields
        if unknown_fields:
            raise ValueError(f"不支持的轨迹字段组: {', '.join(sorted(unknown_fields))}")

        result = await self.repository.get_track_points(
            batch=normalized_batch,
            external_id=normalized_external_id,
            start=start,
            limit=limit,
            start_time=self._datetime_to_source_text(normalized_start_time),
            end_time=self._datetime_to_source_text(normalized_end_time),
            every_nth=safe_every_nth,
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
            "start": max(start, 0),
            "limit": limit,
            "start_time": self._datetime_to_utc_text(normalized_start_time),
            "end_time": self._datetime_to_utc_text(normalized_end_time),
            "every_nth": safe_every_nth,
            "fields": sorted(selected_fields),
        }

        if result is None:
            return self._not_found(
                message="没有找到指定航班的轨迹数据",
                query=query,
            )

        raw_plots = result.get("plots") or []
        points = []
        for source_index, plot in enumerate(raw_plots):
            if source_index % safe_every_nth != 0:
                continue
            point = self._flatten_track_point(
                plot=plot,
                sequence=max(start, 0) + source_index,
            )
            points.append(
                {
                    key: value
                    for key, value in point.items()
                    if key in {"sequence", "timestamp"} or key in selected_fields
                }
            )

        source_points_returned = len(raw_plots)
        total_points = int(result.get("total_points") or 0)
        next_start = max(start, 0) + source_points_returned

        return {
            "status": "ok",
            "query": query,
            "data": {
                "flight": self._format_flight_summary(result),
                "pagination": {
                    "start": result.get(
                        "start",
                        max(start, 0),
                    ),
                    "returned_points": len(points),
                    "source_points_consumed": source_points_returned,
                    "total_points": total_points,
                    "next_start": next_start if next_start < total_points else None,
                    "has_more": next_start < total_points,
                },
                "points": points,
            },
        }

    async def get_fpl_overview(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any]:
        """返回飞行计划事件摘要。"""
        normalized_batch = self._require_text(
            batch,
            field_name="batch",
        )
        normalized_external_id = self._normalize_external_id(external_id)

        result = await self.repository.get_fpl_summary(
            batch=normalized_batch,
            external_id=normalized_external_id,
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
        }

        if result is None:
            return self._not_found(
                message="没有找到指定航班的飞行计划",
                query=query,
            )

        return {
            "status": "ok",
            "query": query,
            "data": self._format_fpl_summary(result),
        }

    async def get_prediction_overview(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any]:
        """返回 SCAT 原始预测轨迹的统计摘要。"""
        normalized_batch = self._require_text(
            batch,
            field_name="batch",
        )
        normalized_external_id = self._normalize_external_id(external_id)

        result = await self.repository.get_prediction_summary(
            batch=normalized_batch,
            external_id=normalized_external_id,
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
        }

        if result is None:
            return self._not_found(
                message="没有找到指定航班的预测轨迹",
                query=query,
            )

        return {
            "status": "ok",
            "query": query,
            "data": self._format_prediction_summary(result),
        }

    async def list_prediction_snapshots(
        self,
        *,
        batch: str,
        external_id: str | int,
        start: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """分页返回 SCAT 原始预测快照目录，不返回完整路线数组。"""
        normalized_batch = self._require_text(batch, field_name="batch")
        normalized_external_id = self._normalize_external_id(external_id)
        result = await self.repository.list_prediction_snapshots(
            batch=normalized_batch,
            external_id=normalized_external_id,
            start=start,
            limit=limit,
        )
        safe_start = max(start, 0)
        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
            "start": safe_start,
            "limit": limit,
        }

        if result is None:
            return self._not_found(
                message="没有找到指定航班",
                query=query,
            )

        snapshots = [
            {
                "time": snapshot.get("time_stamp"),
                "route_point_count": int(snapshot.get("route_point_count") or 0),
            }
            for snapshot in (result.get("snapshots") or [])
        ]
        total = int(result.get("total_snapshots") or 0)

        return {
            "status": "ok",
            "query": query,
            "data": {
                "flight": self._format_flight_summary(result),
                "pagination": {
                    "start": safe_start,
                    "returned_snapshots": len(snapshots),
                    "total_snapshots": total,
                    "has_more": safe_start + len(snapshots) < total,
                },
                "snapshots": self._json_safe(snapshots),
            },
        }

    async def get_prediction_snapshot(
        self,
        *,
        batch: str,
        external_id: str | int,
        timestamp: str,
    ) -> dict[str, Any]:
        """返回指定时刻之前最近的一份 SCAT 预测快照。"""
        normalized_batch = self._require_text(
            batch,
            field_name="batch",
        )
        normalized_external_id = self._normalize_external_id(external_id)
        normalized_timestamp = self._require_text(
            timestamp,
            field_name="timestamp",
        )

        # 提前验证时间格式，防止错误字符串进入查询。
        parsed_timestamp = self._parse_datetime(normalized_timestamp)

        if parsed_timestamp is None:
            raise ValueError("timestamp 必须是 ISO 8601 时间字符串")

        result = await self.repository.get_prediction_snapshot_at_or_before(
            batch=normalized_batch,
            external_id=normalized_external_id,
            timestamp=self._datetime_to_source_text(parsed_timestamp) or normalized_timestamp,
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
            "timestamp": normalized_timestamp,
        }

        if result is None:
            return self._not_found(
                message=("指定时刻之前没有可用的 SCAT 预测轨迹快照"),
                query=query,
            )

        snapshot = result.get("snapshot") or {}
        route = snapshot.get("route") or []

        formatted_route = [
            self._format_route_point(
                route_point=point,
                sequence=index,
            )
            for index, point in enumerate(route)
        ]

        return {
            "status": "ok",
            "query": query,
            "data": {
                "flight": self._format_flight_summary(result),
                "snapshot_time": snapshot.get("time_stamp"),
                "route_point_count": len(formatted_route),
                "route": formatted_route,
            },
        }

    # =========================================================
    # 统计分析方法
    # =========================================================

    async def get_airport_stats(
        self,
        *,
        batch: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """返回一个批次内各起飞机场的航班量与起飞延误统计。"""
        normalized_batch = self._require_text(batch, field_name="batch")
        rows = await self.repository.get_airport_statistics(
            batch=normalized_batch,
            limit=limit,
        )

        query = {"batch": normalized_batch, "limit": limit}
        if not rows:
            return self._not_found(
                message=f"没有找到批次 {normalized_batch} 的机场统计",
                query=query,
            )

        total_flights = sum(int(row.get("flight_count") or 0) for row in rows)
        with_atd = sum(int(row.get("with_atd_count") or 0) for row in rows)
        delayed = sum(int(row.get("delayed_count") or 0) for row in rows)

        def _round(value: Any) -> float | None:
            number = self._to_float(value)
            return round(number, 2) if number is not None else None

        airports = [
            {
                "airport": row.get("airport"),
                "flight_count": int(row.get("flight_count") or 0),
                "with_atd_count": int(row.get("with_atd_count") or 0),
                "mean_delay_minutes": _round(row.get("mean_delay_minutes")),
                "max_delay_minutes": _round(row.get("max_delay_minutes")),
                "min_delay_minutes": _round(row.get("min_delay_minutes")),
                "delayed_count": int(row.get("delayed_count") or 0),
                "delayed_rate": self._ratio(
                    count=int(row.get("delayed_count") or 0),
                    total=int(row.get("with_atd_count") or 0),
                ),
            }
            for row in rows
        ]

        weighted_delay = sum(
            (row["mean_delay_minutes"] or 0.0) * row["with_atd_count"] for row in airports
        )
        overall_mean_delay = round(weighted_delay / with_atd, 2) if with_atd else None

        return {
            "status": "ok",
            "query": query,
            "data": {
                "statistics": {
                    "flight_count": total_flights,
                    "airport_count": len(airports),
                    "with_atd_count": with_atd,
                    "mean_delay_minutes": overall_mean_delay,
                    "delayed_count": delayed,
                    "delayed_rate": self._ratio(count=delayed, total=with_atd),
                    "delay_threshold_minutes": 15,
                },
                "airports": self._json_safe(airports),
            },
        }

    async def get_flight_phase_stats(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any]:
        """返回单航班轨迹按爬升/巡航/下降阶段划分的统计。"""
        normalized_batch = self._require_text(batch, field_name="batch")
        normalized_external_id = self._normalize_external_id(external_id)
        rows = await self.repository.get_trajectory_phase_stats(
            batch=normalized_batch,
            external_id=normalized_external_id,
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
        }
        if not rows:
            return self._not_found(
                message="没有找到指定航班的轨迹数据",
                query=query,
            )

        total_points = sum(int(row.get("point_count") or 0) for row in rows)
        phases = []
        for row in rows:
            phases.append(
                {
                    "vertical_mode": row.get("_id"),
                    "phase_name": self._vertical_mode_name(row.get("_id")),
                    "point_count": int(row.get("point_count") or 0),
                    "point_ratio": self._ratio(
                        count=int(row.get("point_count") or 0),
                        total=total_points,
                    ),
                    "start_time": row.get("start_time"),
                    "end_time": row.get("end_time"),
                    "duration_seconds": self._duration_seconds(
                        start_time=row.get("start_time"),
                        end_time=row.get("end_time"),
                    ),
                    "flight_level": {
                        "minimum": self._to_float(row.get("min_flight_level")),
                        "maximum": self._to_float(row.get("max_flight_level")),
                        "average": self._to_float(row.get("average_flight_level")),
                    },
                    "ground_speed": {
                        "average_mps": self._to_float(row.get("average_ground_speed_mps")),
                        "maximum_mps": self._to_float(row.get("maximum_ground_speed_mps")),
                    },
                    "absolute_vertical_rate": {
                        "average_fpm": self._to_float(row.get("average_abs_rocd_fpm")),
                        "maximum_fpm": self._to_float(row.get("maximum_abs_rocd_fpm")),
                    },
                }
            )

        return {
            "status": "ok",
            "query": query,
            "data": {
                "total_points": total_points,
                "phases": self._json_safe(phases),
            },
        }

    async def get_prediction_error_stats(
        self,
        *,
        batch: str,
        external_id: str | int,
        timestamp: str | None = None,
        match_window_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """计算最新预测快照与实际轨迹之间的水平/垂直/时间误差。

        对每个预测航路点，在实际轨迹中找时间最接近其 ETO 的轨迹点
        （窗口 match_window_seconds 内），分别计算：
        - time_error_s：实际时间 - ETO；
        - horizontal_error_m：预测经纬度与实际经纬度的球面距离；
        - vertical_error_m：实际飞行高度层与预测高度层的差值。
        """
        normalized_batch = self._require_text(batch, field_name="batch")
        normalized_external_id = self._normalize_external_id(external_id)
        normalized_timestamp: str | None = None
        if timestamp is not None:
            parsed = self._parse_datetime(timestamp)
            if parsed is None:
                raise ValueError("timestamp 必须是 ISO 8601 时间字符串")
            normalized_timestamp = self._datetime_to_source_text(parsed)

        snapshot_doc, track_points = await asyncio.gather(
            self.repository.get_latest_prediction_snapshot(
                batch=normalized_batch,
                external_id=normalized_external_id,
                before=normalized_timestamp,
            ),
            self.repository.get_track_arrays_for_error_stats(
                batch=normalized_batch,
                external_id=normalized_external_id,
            ),
        )

        query = {
            "batch": normalized_batch,
            "external_id": normalized_external_id,
            "timestamp": timestamp,
            "match_window_seconds": match_window_seconds,
        }
        if snapshot_doc is None:
            return self._not_found(
                message="没有找到指定航班（或指定时刻之前）的预测轨迹快照",
                query=query,
            )
        if not track_points:
            return self._not_found(
                message="没有找到指定航班的实际轨迹数据",
                query=query,
            )

        snapshot = snapshot_doc.get("last_snapshot") or {}
        route = snapshot.get("route") or []

        parsed_track: list[tuple[datetime, float, float, float | None]] = []
        for point in track_points:
            parsed_time = self._parse_datetime(point.get("timestamp"))
            lat = self._to_float(point.get("lat"))
            lon = self._to_float(point.get("lon"))
            if parsed_time is None or lat is None or lon is None:
                continue
            parsed_track.append(
                (
                    parsed_time,
                    lat,
                    lon,
                    self._to_float(point.get("flight_level")),
                )
            )
        if not parsed_track:
            return self._not_found(
                message="实际轨迹点缺少有效时间或经纬度，无法计算误差",
                query=query,
            )

        track_times = [item[0] for item in parsed_track]
        fixes: list[dict[str, Any]] = []
        for route_point in route:
            eto = self._parse_datetime(route_point.get("eto"))
            predicted_lat = self._to_float(route_point.get("lat"))
            predicted_lon = self._to_float(route_point.get("lon"))
            predicted_fl = self._to_float(route_point.get("afl_value"))
            if eto is None or predicted_lat is None or predicted_lon is None:
                continue
            index = bisect.bisect_left(track_times, eto)
            best: tuple[float, int] | None = None
            for candidate in (index - 1, index):
                if 0 <= candidate < len(parsed_track):
                    delta = abs((track_times[candidate] - eto).total_seconds())
                    if best is None or delta < best[0]:
                        best = (delta, candidate)
            if best is None or best[0] > match_window_seconds:
                continue
            _, track_index = best
            actual_time, actual_lat, actual_lon, actual_fl = parsed_track[track_index]
            horizontal_m = _haversine_metres(
                predicted_lat,
                predicted_lon,
                actual_lat,
                actual_lon,
            )
            vertical_fl = (
                actual_fl - predicted_fl
                if actual_fl is not None and predicted_fl is not None
                else None
            )
            fixes.append(
                {
                    "fix_name": route_point.get("fix_name"),
                    "fix_kind": route_point.get("fix_kind"),
                    "eto": route_point.get("eto"),
                    "matched_time": self._datetime_to_utc_text(actual_time),
                    "time_error_s": round((actual_time - eto).total_seconds(), 2),
                    "horizontal_error_m": round(horizontal_m, 1),
                    "vertical_error_fl": (
                        round(vertical_fl, 2) if vertical_fl is not None else None
                    ),
                    "vertical_error_m": (
                        round(vertical_fl * 30.48, 1) if vertical_fl is not None else None
                    ),
                }
            )

        route_points_with_eto = sum(
            1
            for route_point in route
            if self._parse_datetime(route_point.get("eto")) is not None
            and self._to_float(route_point.get("lat")) is not None
            and self._to_float(route_point.get("lon")) is not None
        )
        matched = len(fixes)
        horizontal_errors = [float(fix["horizontal_error_m"]) for fix in fixes]
        vertical_errors = [
            float(fix["vertical_error_m"]) for fix in fixes if fix["vertical_error_m"] is not None
        ]
        time_errors = [float(fix["time_error_s"]) for fix in fixes]

        def _mean(values: list[float]) -> float | None:
            if not values:
                return None
            return round(sum(values) / len(values), 2)

        def _median(values: list[float]) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            middle = len(ordered) // 2
            if len(ordered) % 2:
                return round(ordered[middle], 2)
            return round((ordered[middle - 1] + ordered[middle]) / 2, 2)

        rmse_horizontal = (
            round(
                math.sqrt(
                    sum(value * value for value in horizontal_errors) / len(horizontal_errors)
                ),
                2,
            )
            if horizontal_errors
            else None
        )

        return {
            "status": "ok",
            "query": query,
            "data": {
                "snapshot_time": snapshot.get("time_stamp"),
                "route_point_count": len(route),
                "track_point_count": len(parsed_track),
                "matched_fix_count": matched,
                "unmatched_fix_count": max(route_points_with_eto - matched, 0),
                "statistics": {
                    "horizontal_error": {
                        "mean_m": _mean(horizontal_errors),
                        "median_m": _median(horizontal_errors),
                        "max_m": (round(max(horizontal_errors), 2) if horizontal_errors else None),
                        "rmse_m": rmse_horizontal,
                    },
                    "vertical_error": {
                        "mean_abs_m": _mean([abs(value) for value in vertical_errors]),
                        "max_abs_m": (
                            round(
                                max(abs(value) for value in vertical_errors),
                                2,
                            )
                            if vertical_errors
                            else None
                        ),
                    },
                    "time_error": {
                        "mean_abs_s": _mean([abs(value) for value in time_errors]),
                        "mean_signed_s": _mean(time_errors),
                        "max_abs_s": (
                            round(
                                max(abs(value) for value in time_errors),
                                2,
                            )
                            if time_errors
                            else None
                        ),
                    },
                },
                "fixes": self._json_safe(fixes[:100]),
            },
        }

    # =========================================================
    # 航班结果格式化
    # =========================================================

    def _format_flight_summary(
        self,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        source = document.get("source") or {}
        identity = document.get("identity") or {}
        ingest = document.get("ingest") or {}
        centre_ctrl = document.get("centre_ctrl") or []

        return self._json_safe(
            {
                "dataset_id": document.get("id"),
                "source": {
                    "system": source.get("system"),
                    "batch": source.get("batch"),
                    "external_id": source.get("external_id"),
                    "external_id_type": source.get("external_id_type"),
                },
                "identity": {
                    "callsign": identity.get("callsign"),
                    "aircraft_type": identity.get("aircraft_type"),
                    "departure_airport": identity.get("adep"),
                    "destination_airport": identity.get("ades"),
                    "iobt_utc": identity.get("iobt_utc"),
                    "atd_utc": identity.get("atd_utc"),
                    "candidate_key": identity.get("candidate_key"),
                    "identity_confidence": identity.get("confidence"),
                },
                "control_centre_transfer_count": max(
                    len(centre_ctrl) - 1,
                    0,
                ),
                "control_centre_history": centre_ctrl,
                "ingest": {
                    "original_filename": ingest.get("original_filename"),
                    "uploaded_at": ingest.get("uploaded_at"),
                },
            }
        )

    def _format_trajectory_summary(
        self,
        summary: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if summary is None:
            return None

        point_count = int(summary.get("point_count") or 0)
        start_time = summary.get("start_time")
        end_time = summary.get("end_time")

        duration_seconds = self._duration_seconds(
            start_time=start_time,
            end_time=end_time,
        )

        flight_level = summary.get("flight_level") or {}
        speed = summary.get("ground_speed_mps") or {}
        rocd = summary.get("absolute_rocd") or {}
        mode_counts = summary.get("vertical_mode_counts") or {}
        quality = summary.get("data_quality") or {}

        minimum_fl = self._to_float(flight_level.get("minimum"))
        maximum_fl = self._to_float(flight_level.get("maximum"))
        average_fl = self._to_float(flight_level.get("average"))

        average_speed_mps = self._to_float(speed.get("average"))
        maximum_speed_mps = self._to_float(speed.get("maximum"))

        average_rocd_fpm = self._to_float(rocd.get("average"))
        maximum_rocd_fpm = self._to_float(rocd.get("maximum"))

        vertical_modes = {
            "level": int(mode_counts.get("level") or 0),
            "climb": int(mode_counts.get("climb") or 0),
            "descent": int(mode_counts.get("descent") or 0),
            "uncertain": int(mode_counts.get("uncertain") or 0),
        }

        vertical_mode_ratios = {
            name: self._ratio(
                count=value,
                total=point_count,
            )
            for name, value in vertical_modes.items()
        }

        return self._json_safe(
            {
                "point_count": point_count,
                "start_time": start_time,
                "end_time": end_time,
                "duration_seconds": duration_seconds,
                "duration_minutes": (
                    round(
                        duration_seconds / 60.0,
                        2,
                    )
                    if duration_seconds is not None
                    else None
                ),
                "geographic_bounds": summary.get("geographic_bounds"),
                "altitude": {
                    "minimum_flight_level": minimum_fl,
                    "maximum_flight_level": maximum_fl,
                    "average_flight_level": average_fl,
                    "minimum_metres": (self._flight_level_to_metres(minimum_fl)),
                    "maximum_metres": (self._flight_level_to_metres(maximum_fl)),
                    "average_metres": (self._flight_level_to_metres(average_fl)),
                },
                "ground_speed": {
                    "average_mps": average_speed_mps,
                    "maximum_mps": maximum_speed_mps,
                    "average_knots": (self._mps_to_knots(average_speed_mps)),
                    "maximum_knots": (self._mps_to_knots(maximum_speed_mps)),
                },
                "absolute_vertical_rate": {
                    "average_fpm": average_rocd_fpm,
                    "maximum_fpm": maximum_rocd_fpm,
                    "average_mps": (self._fpm_to_mps(average_rocd_fpm)),
                    "maximum_mps": (self._fpm_to_mps(maximum_rocd_fpm)),
                },
                "vertical_mode_counts": vertical_modes,
                "vertical_mode_ratios": (vertical_mode_ratios),
                "data_quality": {
                    **quality,
                    "has_duplicate_timestamps": (
                        int(quality.get("duplicate_timestamp_count") or 0) > 0
                    ),
                    "is_model_input_candidate": (
                        point_count >= 72
                        and int(quality.get("missing_position_points") or 0) == 0
                        and int(quality.get("missing_motion_points") or 0) == 0
                    ),
                },
            }
        )

    def _format_fpl_summary(
        self,
        summary: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if summary is None:
            return None

        latest_departure = summary.get("latest_departure") or {}
        latest_arrival = summary.get("latest_arrival") or {}
        latest_clearance = summary.get("latest_clearance") or {}
        latest_holding = summary.get("latest_holding") or {}
        latest_plan = summary.get("latest_plan_update") or {}

        return self._json_safe(
            {
                "event_counts": summary.get("event_counts") or {},
                "departure": {
                    "runway": latest_departure.get("departure_runway"),
                    "sid": latest_departure.get("sid"),
                    "iobt": latest_departure.get("iobt"),
                    "atd": latest_departure.get("atd"),
                    "last_update_time": (latest_departure.get("time_stamp")),
                },
                "arrival": {
                    "runway": latest_arrival.get("arrival_runway"),
                    "star": latest_arrival.get("star"),
                    "approach_clearance": (latest_arrival.get("approach_clearance")),
                    "missed_approach": (latest_arrival.get("missed_approach_flag")),
                    "last_update_time": (latest_arrival.get("time_stamp")),
                },
                "latest_clearance": {
                    "cleared_level": (latest_clearance.get("cfl")),
                    "cleared_level_unit": (latest_clearance.get("cfl_unit")),
                    "assigned_speed": (latest_clearance.get("assigned_speed_val")),
                    "assigned_speed_unit": (latest_clearance.get("assigned_speed_unit")),
                    "assigned_heading": (latest_clearance.get("assigned_heading_val")),
                    "assigned_heading_beacon": (latest_clearance.get("assign_heading_beacon")),
                    "last_update_time": (latest_clearance.get("time_stamp")),
                },
                "holding": {
                    "had_holding": bool(summary.get("had_holding")),
                    "latest_status": (latest_holding.get("holding_status")),
                    "latest_stack_status": (latest_holding.get("holding_stack_status")),
                    "entry_time": (latest_holding.get("holding_entry_time")),
                    "leaving_time": (latest_holding.get("holding_leaving_time")),
                },
                "route_plan": {
                    "icao_route": latest_plan.get("icao_route"),
                    "requested_flight_level": (latest_plan.get("rfl_string")),
                    "true_airspeed": latest_plan.get("tas_string"),
                    "entry_point": latest_plan.get("copn"),
                    "exit_point": latest_plan.get("copx"),
                    "last_update_time": (latest_plan.get("time_stamp")),
                },
                "had_approach_clearance": bool(summary.get("had_approach_clearance")),
            }
        )

    def _format_prediction_summary(
        self,
        summary: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if summary is None:
            return None

        snapshot_count = int(summary.get("snapshot_count") or 0)

        return self._json_safe(
            {
                "available": snapshot_count > 0,
                "snapshot_count": snapshot_count,
                "first_snapshot_time": summary.get("first_snapshot_time"),
                "last_snapshot_time": summary.get("last_snapshot_time"),
                "minimum_route_points": summary.get("minimum_route_points"),
                "maximum_route_points": summary.get("maximum_route_points"),
                "average_route_points": summary.get("average_route_points"),
                "source": "scat_atc_prediction",
                "note": ("这是 SCAT 原始空管预测轨迹，不是 GBAR 模型预测结果。"),
            }
        )

    # =========================================================
    # 轨迹点格式化
    # =========================================================

    def _flatten_track_point(
        self,
        *,
        plot: dict[str, Any],
        sequence: int,
    ) -> dict[str, Any]:
        position = plot.get("I062/105") or {}
        flight_level_data = plot.get("I062/136") or {}
        velocity = plot.get("I062/185") or {}
        status = plot.get("I062/200") or {}
        vertical_rate = plot.get("I062/220") or {}
        mode_s = plot.get("I062/380") or {}

        subitem13 = mode_s.get("subitem13") or {}
        subitem26 = mode_s.get("subitem26") or {}
        subitem27 = mode_s.get("subitem27") or {}
        subitem3 = mode_s.get("subitem3") or {}
        subitem6 = mode_s.get("subitem6") or {}
        subitem7 = mode_s.get("subitem7") or {}

        lat = self._to_float(position.get("lat"))
        lon = self._to_float(position.get("lon"))
        flight_level = self._to_float(flight_level_data.get("measured_flight_level"))
        vx = self._to_float(velocity.get("vx"))
        vy = self._to_float(velocity.get("vy"))
        rocd_fpm = self._to_float(vertical_rate.get("rocd"))

        ground_speed_mps: float | None = None
        ground_speed_knots: float | None = None
        track_angle_deg: float | None = None

        if vx is not None and vy is not None:
            ground_speed_mps = round(
                math.hypot(vx, vy),
                4,
            )
            ground_speed_knots = self._mps_to_knots(ground_speed_mps)

            track_angle_deg = round(
                (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0,
                4,
            )

        missing_fields: list[str] = []

        required_fields = {
            "timestamp": plot.get("time_of_track"),
            "lat": lat,
            "lon": lon,
            "flight_level": flight_level,
            "vx": vx,
            "vy": vy,
            "rocd": rocd_fpm,
            "vertical_mode": status.get("vert"),
        }

        for field_name, value in required_fields.items():
            if value is None:
                missing_fields.append(field_name)

        return self._json_safe(
            {
                "sequence": sequence,
                "timestamp": plot.get("time_of_track"),
                "position": {
                    "lat_deg": lat,
                    "lon_deg": lon,
                    "flight_level": flight_level,
                    "altitude_ft": (
                        round(
                            flight_level * 100.0,
                            2,
                        )
                        if flight_level is not None
                        else None
                    ),
                    "altitude_m": (self._flight_level_to_metres(flight_level)),
                },
                "motion": {
                    "vx_mps": vx,
                    "vy_mps": vy,
                    "ground_speed_mps": (ground_speed_mps),
                    "ground_speed_knots": (ground_speed_knots),
                    "track_angle_deg": (track_angle_deg),
                    "rocd_fpm": rocd_fpm,
                    "vertical_speed_mps": (self._fpm_to_mps(rocd_fpm)),
                    "vertical_mode": status.get("vert"),
                    "vertical_mode_name": (self._vertical_mode_name(status.get("vert"))),
                },
                "mode_s": {
                    "available": bool(mode_s),
                    "barometric_vertical_rate_fpm": (subitem13.get("baro_vert_rate")),
                    "indicated_airspeed_knots": (subitem26.get("ias")),
                    "mach": subitem27.get("mach"),
                    "magnetic_heading_deg": (subitem3.get("mag_hdg")),
                    "selected_altitude": (subitem6.get("altitude")),
                    "mode_s_altitude_ft": (subitem7.get("altitude")),
                },
                "quality": {
                    "valid_for_model_input": (len(missing_fields) == 0),
                    "missing_fields": missing_fields,
                },
            }
        )

    def _format_route_point(
        self,
        *,
        route_point: dict[str, Any],
        sequence: int,
    ) -> dict[str, Any]:
        flight_level = self._to_float(route_point.get("afl_value"))

        return self._json_safe(
            {
                "sequence": sequence,
                "fix_name": route_point.get("fix_name"),
                "fix_kind": route_point.get("fix_kind"),
                "lat_deg": route_point.get("lat"),
                "lon_deg": route_point.get("lon"),
                "estimated_time_over": (route_point.get("eto")),
                "actual_flight_level": (flight_level),
                "actual_altitude_m": (self._flight_level_to_metres(flight_level)),
                "requested_flight_level": (route_point.get("rfl_value")),
                "is_actual_time_over": bool(route_point.get("is_ato")),
            }
        )

    # =========================================================
    # 通用辅助方法
    # =========================================================

    @staticmethod
    def _require_text(
        value: str,
        *,
        field_name: str,
    ) -> str:
        normalized = value.strip()

        if not normalized:
            raise ValueError(f"{field_name} 不能为空")

        return normalized

    @staticmethod
    def _optional_text(
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @classmethod
    def _optional_code(
        cls,
        value: str | None,
    ) -> str | None:
        normalized = cls._optional_text(value)

        if normalized is None:
            return None

        return normalized.upper()

    @classmethod
    def _optional_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                normalized
                for value in values
                if (normalized := cls._optional_code(value)) is not None
            )
        )

    @classmethod
    def _optional_texts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                normalized
                for value in values
                if (normalized := cls._optional_text(value)) is not None
            )
        )

    @staticmethod
    def _normalize_external_id(
        external_id: str | int,
    ) -> str:
        normalized = str(external_id).strip()

        if not normalized:
            raise ValueError("external_id 不能为空")

        return normalized

    @staticmethod
    def _to_float(
        value: Any,
    ) -> float | None:
        if value is None or isinstance(value, bool):
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_datetime(
        value: Any,
    ) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            normalized = value.strip()

            if not normalized:
                return None

            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"

            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
        else:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)

        return parsed

    @classmethod
    def _normalize_optional_datetime(
        cls,
        value: str | None,
        field_name: str,
    ) -> datetime | None:
        if value is None:
            return None
        parsed = cls._parse_datetime(value)
        if parsed is None:
            raise ValueError(f"{field_name} 必须是 ISO 8601 时间字符串")
        return parsed.astimezone(UTC)

    @staticmethod
    def _datetime_to_utc_text(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _datetime_to_source_text(value: datetime | None) -> str | None:
        """Serialize UTC using the timezone-naive format stored in SCAT arrays."""
        if value is None:
            return None
        return value.astimezone(UTC).replace(tzinfo=None).isoformat()

    @classmethod
    def _duration_seconds(
        cls,
        *,
        start_time: Any,
        end_time: Any,
    ) -> float | None:
        start = cls._parse_datetime(start_time)
        end = cls._parse_datetime(end_time)

        if start is None or end is None:
            return None

        duration = (end - start).total_seconds()

        if duration < 0:
            return None

        return round(duration, 3)

    @staticmethod
    def _flight_level_to_metres(
        flight_level: float | None,
    ) -> float | None:
        if flight_level is None:
            return None

        # 一个 flight level 单位等于 100 英尺。
        return round(
            flight_level * 100.0 * FEET_TO_METRES,
            3,
        )

    @staticmethod
    def _mps_to_knots(
        speed_mps: float | None,
    ) -> float | None:
        if speed_mps is None:
            return None

        return round(
            speed_mps * MPS_TO_KNOTS,
            3,
        )

    @staticmethod
    def _fpm_to_mps(
        vertical_rate_fpm: float | None,
    ) -> float | None:
        if vertical_rate_fpm is None:
            return None

        return round(
            vertical_rate_fpm * FPM_TO_MPS,
            4,
        )

    @staticmethod
    def _ratio(
        *,
        count: int,
        total: int,
    ) -> float:
        if total <= 0:
            return 0.0

        return round(count / total, 4)

    @staticmethod
    def _vertical_mode_name(
        mode: Any,
    ) -> str | None:
        mapping = {
            0: "level",
            1: "climb",
            2: "descent",
            3: "uncertain",
        }

        try:
            return mapping.get(int(mode))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _json_safe(
        cls,
        value: Any,
    ) -> Any:
        """递归转换为可由 FastAPI/JSON 编码的结构。"""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)

            return value.isoformat().replace(
                "+00:00",
                "Z",
            )

        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}

        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(item) for item in value]

        return value

    @staticmethod
    def _not_found(
        *,
        message: str,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "not_found",
            "message": message,
            "query": query,
            "data": None,
        }
