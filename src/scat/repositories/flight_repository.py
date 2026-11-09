import asyncio
import re
from datetime import datetime
from typing import Any, Final

from core.settings import settings
from scat.infrastructure.database import flight_database
from scat.preprocessing.gbar_v35 import (
    FLIGHT_LEVEL_TO_METRES,
    FPM_TO_MPS,
    SCAT_GBAR_ENCODING,
    SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES,
    SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS,
    SCAT_GBAR_QUALITY_POLICY_VERSION,
    SCAT_GBAR_SPIKE_THRESHOLDS,
)

SCAT_SOURCE_SYSTEM: Final[str] = "scat-public-dataset"

# GBAR v35 全库质量聚合：六特征的物理范围（原始单位）与相邻点跳变阈值。
# 跳变阈值由预处理器的转换单位换算到 MongoDB 原始单位，口径保持一致：
#   - 高度层跳变 600 m（600 / 30.48 百英尺）；
#   - rocd 跳变 20 m/s（20 / 0.00508 英尺/分钟）；
#   - lat/lon 不设标量阈值，按 haversine 三点孤立跳变判断（4 km）。
_GBAR_QUALITY_FEATURES: Final[dict[str, dict[str, Any]]] = {
    "lat": {
        "path": "I062/105.lat",
        "minimum": SCAT_GBAR_ENCODING["lat"].minimum,
        "maximum": SCAT_GBAR_ENCODING["lat"].maximum,
        "spike_threshold": None,
    },
    "lon": {
        "path": "I062/105.lon",
        "minimum": SCAT_GBAR_ENCODING["lon"].minimum,
        "maximum": SCAT_GBAR_ENCODING["lon"].maximum,
        "spike_threshold": None,
    },
    "measured_flight_level": {
        "path": "I062/136.measured_flight_level",
        "minimum": SCAT_GBAR_ENCODING["measured_flight_level"].minimum / FLIGHT_LEVEL_TO_METRES,
        "maximum": SCAT_GBAR_ENCODING["measured_flight_level"].maximum / FLIGHT_LEVEL_TO_METRES,
        "spike_threshold": SCAT_GBAR_SPIKE_THRESHOLDS["measured_flight_level"]
        / FLIGHT_LEVEL_TO_METRES,
    },
    "vx": {
        "path": "I062/185.vx",
        "minimum": SCAT_GBAR_ENCODING["vx"].minimum,
        "maximum": SCAT_GBAR_ENCODING["vx"].maximum,
        "spike_threshold": SCAT_GBAR_SPIKE_THRESHOLDS["vx"],
    },
    "vy": {
        "path": "I062/185.vy",
        "minimum": SCAT_GBAR_ENCODING["vy"].minimum,
        "maximum": SCAT_GBAR_ENCODING["vy"].maximum,
        "spike_threshold": SCAT_GBAR_SPIKE_THRESHOLDS["vy"],
    },
    "rocd": {
        "path": "I062/220.rocd",
        "minimum": SCAT_GBAR_ENCODING["rocd"].minimum / FPM_TO_MPS,
        "maximum": SCAT_GBAR_ENCODING["rocd"].maximum / FPM_TO_MPS,
        "spike_threshold": SCAT_GBAR_SPIKE_THRESHOLDS["rocd"] / FPM_TO_MPS,
    },
}
_GBAR_QUALITY_POSITION_SPIKE_METRES: Final[float] = SCAT_GBAR_MAXIMUM_POSITION_JUMP_METRES
_GBAR_QUALITY_EARTH_RADIUS_METRES: Final[float] = 6_371_000.0
# 相邻两腿经纬度差都小于该阈值时，每腿距离 < 约 3.1 km，一定不是 4 km 跳变，
# 可跳过三角计算；仅用于加速，不影响判定结果。
_GBAR_QUALITY_SPIKE_SKIP_DEGREES: Final[float] = 0.02
_GBAR_QUALITY_SORT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "outlier_point_count",
        "missing_point_count",
        "spike_value_count",
        "point_count",
        "external_id",
    }
)
# 全库质量聚合按航班 id 取模分区并行执行；实测 8 分区把批次扫描从 126s 降到 18s。
_GBAR_QUALITY_PARTITIONS: Final[int] = 8
_GBAR_QUALITY_CLEAN_SAMPLE_LIMIT: Final[int] = 5
_GBAR_QUALITY_SCAN_SEMAPHORE = asyncio.Semaphore(1)

# 普通列表和摘要查询不得返回两个大数组。
FLIGHT_SUMMARY_PROJECTION: Final[dict[str, int]] = {
    "_id": 0,
    "id": 1,
    "source": 1,
    "identity": 1,
    "centre_ctrl": 1,
    "ingest.original_filename": 1,
    "ingest.uploaded_at": 1,
}


class ScatFlightRepository:
    """SCAT 航班数据访问层。

    该类只负责 MongoDB 查询，不负责：
    - 自然语言处理
    - 单位转换
    - 轨迹特征计算
    - 模型预测
    - Agent 回答组织
    """

    @property
    def collection(self) -> Any:
        """获取 scat_flights 集合。

        调用 Repository 之前，必须先执行：
            await flight_database.connect()
        """
        return flight_database.get_collection(settings.SCAT_FLIGHTS_COLLECTION)

    @property
    def analytics_collection(self) -> Any:
        """长耗时质量聚合专用句柄（无客户端 CSOT 超时，服务端 maxTimeMS 生效）。"""
        return flight_database.get_analytics_collection(settings.SCAT_FLIGHTS_COLLECTION)

    @staticmethod
    def _source_filter(
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any]:
        """构造按来源唯一定位航班的查询条件。"""
        normalized_batch = batch.strip()

        if not normalized_batch:
            raise ValueError("batch 不能为空")

        return {
            "source.system": SCAT_SOURCE_SYSTEM,
            "source.batch": normalized_batch,
            # 数据库中的 external_id 是字符串。
            "source.external_id": str(external_id).strip(),
        }

    @staticmethod
    def _safe_limit(
        limit: int,
        *,
        default: int = 20,
        maximum: int = 100,
    ) -> int:
        """限制查询返回数量，防止一次返回过多文档。"""
        if limit <= 0:
            return default

        return min(limit, maximum)

    @staticmethod
    def _safe_skip(skip: int) -> int:
        """确保分页偏移量不小于 0。"""
        return max(skip, 0)

    @staticmethod
    def _add_selection_field(
        filters: dict[str, Any],
        *,
        path: str,
        exact: str | None = None,
        allowed: tuple[str, ...] = (),
        denied: tuple[str, ...] = (),
        prefix: str | None = None,
    ) -> None:
        """Add one bounded exact/list/exclusion/prefix condition."""
        if exact is not None:
            filters[path] = exact
            return
        condition: dict[str, Any] = {}
        if allowed:
            condition["$in"] = list(allowed)
        if denied:
            condition["$nin"] = list(denied)
            # MongoDB 的 $nin 也匹配字段不存在的文档；否定筛选应表示
            # “字段有已知值且不在排除列表中”。
            condition["$exists"] = True
        if prefix:
            condition["$regex"] = f"^{re.escape(prefix)}"
        if condition:
            filters[path] = condition

    @staticmethod
    def _build_search_filter(
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
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        min_track_points: int | None = None,
        max_track_points: int | None = None,
        has_fpl: bool | None = None,
        has_scat_prediction: bool | None = None,
    ) -> dict[str, Any]:
        """构造航班列表和计数共用的结构化筛选条件。"""
        filters: dict[str, Any] = {
            "source.system": SCAT_SOURCE_SYSTEM,
        }

        if batch:
            filters["source.batch"] = batch.strip()
        ScatFlightRepository._add_selection_field(
            filters,
            path="identity.callsign",
            exact=callsign,
            allowed=callsigns,
            denied=exclude_callsigns,
            prefix=callsign_prefix,
        )
        ScatFlightRepository._add_selection_field(
            filters,
            path="identity.aircraft_type",
            exact=aircraft_type,
            allowed=aircraft_types,
            denied=exclude_aircraft_types,
        )
        ScatFlightRepository._add_selection_field(
            filters,
            path="identity.adep",
            exact=adep,
            allowed=adeps,
            denied=exclude_adeps,
        )
        ScatFlightRepository._add_selection_field(
            filters,
            path="identity.ades",
            exact=ades,
            allowed=adess,
            denied=exclude_adess,
        )
        ScatFlightRepository._add_selection_field(
            filters,
            path="source.external_id",
            exact=external_id,
            allowed=external_ids,
            denied=exclude_external_ids,
        )
        if candidate_key:
            filters["identity.candidate_key"] = candidate_key.strip()
        if start_time or end_time:
            time_filter: dict[str, datetime] = {}
            if start_time:
                time_filter["$gte"] = start_time
            if end_time:
                time_filter["$lt"] = end_time
            filters["identity.iobt_utc"] = time_filter

        expressions: list[dict[str, Any]] = []
        plot_count = {"$size": {"$ifNull": ["$plots", []]}}
        if min_track_points is not None:
            expressions.append({"$gte": [plot_count, min_track_points]})
        if max_track_points is not None:
            expressions.append({"$lte": [plot_count, max_track_points]})
        if has_scat_prediction is not None:
            prediction_count = {"$size": {"$ifNull": ["$predicted_trajectory", []]}}
            operator = "$gt" if has_scat_prediction else "$eq"
            expressions.append({operator: [prediction_count, 0]})
        if has_fpl is not None:
            fpl_field_count = {"$size": {"$objectToArray": {"$ifNull": ["$fpl", {}]}}}
            operator = "$gt" if has_fpl else "$eq"
            expressions.append({operator: [fpl_field_count, 0]})
        if expressions:
            filters["$expr"] = expressions[0] if len(expressions) == 1 else {"$and": expressions}

        return filters

    async def count_all(
        self,
        *,
        batch: str | None = None,
    ) -> int:
        """统计 SCAT 航班数量。

        batch 为 None 时统计全部 SCAT 航班；
        指定 batch 时仅统计该批次。
        """
        filters: dict[str, Any] = {
            "source.system": SCAT_SOURCE_SYSTEM,
        }

        if batch:
            filters["source.batch"] = batch.strip()

        return await self.collection.count_documents(filters)

    async def count_batches(self) -> int:
        """统计 SCAT 数据批次数量。"""
        pipeline: list[dict[str, Any]] = [
            {"$match": {"source.system": SCAT_SOURCE_SYSTEM}},
            {"$group": {"_id": "$source.batch"}},
            {"$count": "count"},
        ]
        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        return int(documents[0]["count"]) if documents else 0

    async def count_flights(
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
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        min_track_points: int | None = None,
        max_track_points: int | None = None,
        has_fpl: bool | None = None,
        has_scat_prediction: bool | None = None,
    ) -> int:
        """统计符合结构化筛选条件的航班数量。"""
        filters = self._build_search_filter(
            batch=batch,
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
            start_time=start_time,
            end_time=end_time,
            min_track_points=min_track_points,
            max_track_points=max_track_points,
            has_fpl=has_fpl,
            has_scat_prediction=has_scat_prediction,
        )
        return await self.collection.count_documents(filters)

    async def get_summary_by_source(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any] | None:
        """根据批次和来源编号查询一条航班摘要。

        不返回 plots 和 predicted_trajectory。
        """
        return await self.collection.find_one(
            self._source_filter(
                batch=batch,
                external_id=external_id,
            ),
            FLIGHT_SUMMARY_PROJECTION,
        )

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
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        min_track_points: int | None = None,
        max_track_points: int | None = None,
        has_fpl: bool | None = None,
        has_scat_prediction: bool | None = None,
        sort_by: str = "iobt",
        sort_order: str = "asc",
        limit: int = 20,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """按结构化条件搜索航班。

        所有条件之间为 AND 关系。

        参数：
            batch:
                数据批次，例如 scat20161015_20161021。
            callsign:
                航班呼号，例如 SAS1755。
            aircraft_type:
                ICAO 机型代码，例如 B738。
            adep:
                起飞机场 ICAO 代码，例如 EGKK。
            ades:
                目的机场 ICAO 代码，例如 ESSA。
            candidate_key:
                候选航班键。该字段有索引，但不保证绝对唯一。
            limit:
                最多返回 100 条。
            skip:
                分页偏移量。
        """
        filters = self._build_search_filter(
            batch=batch,
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
            start_time=start_time,
            end_time=end_time,
            min_track_points=min_track_points,
            max_track_points=max_track_points,
            has_fpl=has_fpl,
            has_scat_prediction=has_scat_prediction,
        )

        safe_limit = self._safe_limit(limit)
        safe_skip = self._safe_skip(skip)

        cursor = self.collection.find(
            filters,
            FLIGHT_SUMMARY_PROJECTION,
        )

        sort_fields = {
            "iobt": "identity.iobt_utc",
            "callsign": "identity.callsign",
            "external_id": "source.external_id",
        }
        primary_field = sort_fields.get(sort_by, "identity.iobt_utc")
        direction = -1 if sort_order == "desc" else 1
        stable_sort = [(primary_field, direction)]
        for field in ("source.batch", "source.external_id"):
            if field != primary_field:
                stable_sort.append((field, direction))
        cursor = cursor.sort(stable_sort)

        cursor = cursor.skip(safe_skip).limit(safe_limit)

        return await cursor.to_list(length=safe_limit)

    async def get_by_candidate_key(
        self,
        *,
        candidate_key: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """根据候选航班键查询。

        candidate_key 不是唯一键，因此返回列表。
        """
        normalized_key = candidate_key.strip()

        if not normalized_key:
            raise ValueError("candidate_key 不能为空")

        safe_limit = self._safe_limit(limit)

        cursor = self.collection.find(
            {
                "source.system": SCAT_SOURCE_SYSTEM,
                "identity.candidate_key": normalized_key,
            },
            FLIGHT_SUMMARY_PROJECTION,
        ).limit(safe_limit)

        return await cursor.to_list(length=safe_limit)

    async def get_track_points(
        self,
        *,
        batch: str,
        external_id: str | int,
        start: int = 0,
        limit: int = 500,
        start_time: str | None = None,
        end_time: str | None = None,
        every_nth: int = 1,
    ) -> dict[str, Any] | None:
        """获取某个航班的一段轨迹点。

        不会默认返回全部 plots，而是使用 start 和 limit 截取数组。

        返回示例：
        {
            "id": 100016,
            "source": {...},
            "identity": {...},
            "total_points": 509,
            "start": 0,
            "returned_points": 100,
            "plots": [...]
        }
        """
        safe_start = max(start, 0)
        safe_limit = self._safe_limit(
            limit,
            default=500,
            maximum=500,
        )
        safe_every_nth = min(max(every_nth, 1), 20)
        source_limit = safe_limit * safe_every_nth

        source_plots: dict[str, Any] = {"$ifNull": ["$plots", []]}
        time_conditions: list[dict[str, Any]] = []
        if start_time:
            time_conditions.append({"$gte": ["$$plot.time_of_track", start_time]})
        if end_time:
            time_conditions.append({"$lt": ["$$plot.time_of_track", end_time]})

        filtered_plots: dict[str, Any] = source_plots
        if time_conditions:
            condition = (
                time_conditions[0] if len(time_conditions) == 1 else {"$and": time_conditions}
            )
            filtered_plots = {
                "$filter": {
                    "input": source_plots,
                    "as": "plot",
                    "cond": condition,
                }
            }

        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._source_filter(
                    batch=batch,
                    external_id=external_id,
                )
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "filtered_plots": filtered_plots,
                }
            },
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "total_points": {"$size": "$filtered_plots"},
                    "plots": {
                        "$slice": [
                            "$filtered_plots",
                            safe_start,
                            source_limit,
                        ]
                    },
                }
            },
            {"$limit": 1},
        ]

        # PyMongo异步API中的aggregate()是协程，必须先await。
        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        if not documents:
            return None

        result = documents[0]
        plots = result.get("plots") or []

        result["start"] = safe_start
        result["returned_points"] = len(plots)
        result["every_nth"] = safe_every_nth

        return result

    async def get_gbar_input_candidates(
        self,
        *,
        batch: str,
        external_id: str | int,
        end_time: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any] | None:
        """Return a bounded, model-only tail of raw SCAT feature rows.

        Unit conversion and quality decisions deliberately remain in the
        preprocessing layer.  The repository only projects the six fields
        required by GBAR plus timestamp and original array position.
        """
        safe_limit = self._safe_limit(limit, default=500, maximum=2_000)
        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "plots": {"$ifNull": ["$plots", []]},
                }
            },
            {
                "$unwind": {
                    "path": "$plots",
                    "includeArrayIndex": "source_index",
                }
            },
        ]
        if end_time:
            pipeline.append({"$match": {"plots.time_of_track": {"$lte": end_time}}})
        pipeline.extend(
            [
                {
                    "$project": {
                        "id": 1,
                        "source": 1,
                        "identity": 1,
                        "point": {
                            "source_index": "$source_index",
                            "timestamp": "$plots.time_of_track",
                            "lat": "$plots.I062/105.lat",
                            "lon": "$plots.I062/105.lon",
                            "measured_flight_level": ("$plots.I062/136.measured_flight_level"),
                            "vx": "$plots.I062/185.vx",
                            "vy": "$plots.I062/185.vy",
                            "rocd": "$plots.I062/220.rocd",
                        },
                    }
                },
                {"$sort": {"point.timestamp": -1, "point.source_index": -1}},
                {"$limit": safe_limit},
                {"$sort": {"point.timestamp": 1, "point.source_index": 1}},
                {
                    "$group": {
                        "_id": None,
                        "id": {"$first": "$id"},
                        "source": {"$first": "$source"},
                        "identity": {"$first": "$identity"},
                        "points": {"$push": "$point"},
                    }
                },
                {"$project": {"_id": 0}},
            ]
        )
        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        return documents[0] if documents else None

    async def get_gbar_future_positions(
        self,
        *,
        batch: str,
        external_id: str | int,
        after_time: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded ground-truth positions after a historical forecast origin."""
        safe_limit = self._safe_limit(limit, default=100, maximum=200)
        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {"$project": {"_id": 0, "plots": {"$ifNull": ["$plots", []]}}},
            {"$unwind": {"path": "$plots", "includeArrayIndex": "source_index"}},
            {"$match": {"plots.time_of_track": {"$gt": after_time}}},
            {"$sort": {"plots.time_of_track": 1, "source_index": 1}},
            {"$limit": safe_limit},
            {
                "$project": {
                    "_id": 0,
                    "source_index": 1,
                    "timestamp": "$plots.time_of_track",
                    "lat": "$plots.I062/105.lat",
                    "lon": "$plots.I062/105.lon",
                    "measured_flight_level": "$plots.I062/136.measured_flight_level",
                    "vx": "$plots.I062/185.vx",
                    "vy": "$plots.I062/185.vy",
                    "rocd": "$plots.I062/220.rocd",
                }
            },
        ]
        cursor = await self.collection.aggregate(pipeline)
        return await cursor.to_list(length=safe_limit)

    async def sample_gbar_flight_ids(
        self,
        *,
        batch: str,
        sample_size: int,
        observation_points: int,
        maximum_sample_size: int = 16,
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
        end_time: str | None = None,
        future_points: int = 100,
    ) -> dict[str, Any]:
        """Randomly sample bounded GBAR candidates with enough observation/truth rows."""
        normalized_batch = batch.strip()
        if not normalized_batch:
            raise ValueError("batch 不能为空")
        safe_maximum_sample_size = self._safe_limit(
            maximum_sample_size,
            default=16,
            maximum=50,
        )
        safe_sample_size = self._safe_limit(
            sample_size,
            default=1,
            maximum=safe_maximum_sample_size,
        )
        safe_observation_points = self._safe_limit(
            observation_points,
            default=300,
            maximum=2_000,
        )
        safe_future_points = self._safe_limit(future_points, default=100, maximum=200)
        plots = {"$ifNull": ["$plots", []]}
        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._build_search_filter(
                    batch=normalized_batch,
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
                    start_time=takeoff_start,
                    end_time=takeoff_end,
                    min_track_points=min_track_points,
                    max_track_points=max_track_points,
                    has_fpl=has_fpl,
                    has_scat_prediction=has_scat_prediction,
                )
            }
        ]
        if end_time is None:
            pipeline.append(
                {
                    "$match": {
                        "$expr": {
                            "$gte": [
                                {"$size": plots},
                                safe_observation_points + safe_future_points,
                            ]
                        }
                    }
                }
            )
        else:
            pipeline.extend(
                [
                    {
                        "$project": {
                            "external_id": "$source.external_id",
                            "observation_count": {
                                "$size": {
                                    "$filter": {
                                        "input": plots,
                                        "as": "plot",
                                        "cond": {"$lte": ["$$plot.time_of_track", end_time]},
                                    }
                                }
                            },
                            "future_count": {
                                "$size": {
                                    "$filter": {
                                        "input": plots,
                                        "as": "plot",
                                        "cond": {"$gt": ["$$plot.time_of_track", end_time]},
                                    }
                                }
                            },
                        }
                    },
                    {
                        "$match": {
                            "observation_count": {"$gte": safe_observation_points},
                            "future_count": {"$gte": safe_future_points},
                        }
                    },
                ]
            )
        external_id_projection = "$source.external_id" if end_time is None else "$external_id"
        pipeline.append(
            {
                "$facet": {
                    "selection": [
                        {"$sample": {"size": safe_sample_size}},
                        {
                            "$project": {
                                "_id": 0,
                                "external_id": external_id_projection,
                            }
                        },
                    ],
                    "summary": [{"$count": "eligible_count"}],
                }
            }
        )
        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        document = documents[0] if documents else {}
        rows = document.get("selection") or []
        summary = document.get("summary") or []
        return {
            "external_ids": [
                str(row["external_id"]) for row in rows if row.get("external_id") is not None
            ],
            "eligible_count": int(summary[0].get("eligible_count") or 0) if summary else 0,
        }

    async def get_fpl(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any] | None:
        """获取一条航班的完整飞行计划信息。"""
        return await self.collection.find_one(
            self._source_filter(
                batch=batch,
                external_id=external_id,
            ),
            {
                "_id": 0,
                "id": 1,
                "source": 1,
                "identity": 1,
                "fpl": 1,
            },
        )

    async def get_centre_control_history(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any] | None:
        """获取航班的管制中心移交历史。"""
        return await self.collection.find_one(
            self._source_filter(
                batch=batch,
                external_id=external_id,
            ),
            {
                "_id": 0,
                "id": 1,
                "source": 1,
                "identity": 1,
                "centre_ctrl": 1,
            },
        )

    async def get_predicted_trajectory(
        self,
        *,
        batch: str,
        external_id: str | int,
        start: int = 0,
        limit: int = 20,
    ) -> dict[str, Any] | None:
        """获取SCAT空管系统的预测轨迹快照。

        predicted_trajectory 是原始SCAT数据中的空管系统预测，
        不是后续接入的GBAR模型预测。

        每个快照通常包含：
            time_stamp
            route
        """
        safe_start = max(start, 0)
        safe_limit = self._safe_limit(
            limit,
            default=20,
            maximum=100,
        )

        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._source_filter(
                    batch=batch,
                    external_id=external_id,
                )
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "total_snapshots": {
                        "$size": {
                            "$ifNull": [
                                "$predicted_trajectory",
                                [],
                            ]
                        }
                    },
                    "predicted_trajectory": {
                        "$slice": [
                            {
                                "$ifNull": [
                                    "$predicted_trajectory",
                                    [],
                                ]
                            },
                            safe_start,
                            safe_limit,
                        ]
                    },
                }
            },
            {"$limit": 1},
        ]

        # aggregate()需要await后才能获得异步游标。
        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        if not documents:
            return None

        result = documents[0]
        snapshots = result.get("predicted_trajectory") or []

        result["start"] = safe_start
        result["returned_snapshots"] = len(snapshots)

        return result

    async def list_batches(
        self,
        *,
        limit: int = 100,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """列出SCAT数据批次及其航班数量和时间范围。"""
        safe_limit = self._safe_limit(
            limit,
            default=100,
            maximum=1000,
        )
        safe_skip = self._safe_skip(skip)

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                }
            },
            {
                "$group": {
                    "_id": "$source.batch",
                    "flight_count": {"$sum": 1},
                    "earliest_iobt_utc": {"$min": "$identity.iobt_utc"},
                    "latest_iobt_utc": {"$max": "$identity.iobt_utc"},
                    "earliest_upload_time": {"$min": "$ingest.uploaded_at"},
                    "latest_upload_time": {"$max": "$ingest.uploaded_at"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "batch": "$_id",
                    "flight_count": 1,
                    "earliest_iobt_utc": 1,
                    "latest_iobt_utc": 1,
                    "earliest_upload_time": 1,
                    "latest_upload_time": 1,
                }
            },
            {"$sort": {"batch": 1}},
            {"$skip": safe_skip},
            {"$limit": safe_limit},
        ]

        cursor = await self.collection.aggregate(pipeline)
        return await cursor.to_list(length=safe_limit)

    async def get_batch_statistics(
        self,
        *,
        batch: str,
    ) -> dict[str, Any] | None:
        """统计一个SCAT批次的总体情况。"""
        normalized_batch = batch.strip()

        if not normalized_batch:
            raise ValueError("batch不能为空")

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                    "source.batch": normalized_batch,
                }
            },
            {
                "$project": {
                    "callsign": "$identity.callsign",
                    "aircraft_type": "$identity.aircraft_type",
                    "adep": "$identity.adep",
                    "ades": "$identity.ades",
                    "plot_count": {"$size": {"$ifNull": ["$plots", []]}},
                    "prediction_snapshot_count": {
                        "$size": {
                            "$ifNull": [
                                "$predicted_trajectory",
                                [],
                            ]
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "flight_count": {"$sum": 1},
                    "callsigns": {"$addToSet": "$callsign"},
                    "aircraft_types": {"$addToSet": "$aircraft_type"},
                    "departure_airports": {"$addToSet": "$adep"},
                    "arrival_airports": {"$addToSet": "$ades"},
                    "total_track_points": {"$sum": "$plot_count"},
                    "average_track_points": {"$avg": "$plot_count"},
                    "minimum_track_points": {"$min": "$plot_count"},
                    "maximum_track_points": {"$max": "$plot_count"},
                    "flights_with_prediction": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        "$prediction_snapshot_count",
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_prediction_snapshots": {"$sum": "$prediction_snapshot_count"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "batch": {"$literal": normalized_batch},
                    "flight_count": 1,
                    "unique_callsign_count": {"$size": "$callsigns"},
                    "aircraft_type_count": {"$size": "$aircraft_types"},
                    "departure_airport_count": {"$size": "$departure_airports"},
                    "arrival_airport_count": {"$size": "$arrival_airports"},
                    "total_track_points": 1,
                    "average_track_points": {"$round": ["$average_track_points", 2]},
                    "minimum_track_points": 1,
                    "maximum_track_points": 1,
                    "flights_with_prediction": 1,
                    "total_prediction_snapshots": 1,
                }
            },
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        return documents[0] if documents else None

    async def get_route_distribution(
        self,
        *,
        batch: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """统计起飞机场到目的机场的航线分布。"""
        match_filter: dict[str, Any] = {
            "source.system": SCAT_SOURCE_SYSTEM,
            "identity.adep": {"$type": "string"},
            "identity.ades": {"$type": "string"},
        }

        if batch:
            match_filter["source.batch"] = batch.strip()

        safe_limit = self._safe_limit(
            limit,
            default=20,
            maximum=100,
        )

        pipeline: list[dict[str, Any]] = [
            {"$match": match_filter},
            {
                "$group": {
                    "_id": {
                        "adep": "$identity.adep",
                        "ades": "$identity.ades",
                    },
                    "flight_count": {"$sum": 1},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "adep": "$_id.adep",
                    "ades": "$_id.ades",
                    "flight_count": 1,
                }
            },
            {
                "$sort": {
                    "flight_count": -1,
                    "adep": 1,
                    "ades": 1,
                }
            },
            {"$limit": safe_limit},
        ]

        cursor = await self.collection.aggregate(pipeline)
        return await cursor.to_list(length=safe_limit)

    async def get_flight_volume(
        self,
        *,
        batch: str,
        granularity: str = "day",
    ) -> list[dict[str, Any]]:
        """按 UTC 小时或日期统计批次内的计划航班量。"""
        normalized_batch = batch.strip()
        if not normalized_batch:
            raise ValueError("batch不能为空")
        if granularity not in {"hour", "day"}:
            raise ValueError("granularity必须为hour或day")

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                    "source.batch": normalized_batch,
                }
            },
            {
                "$project": {
                    "iobt": {
                        "$convert": {
                            "input": "$identity.iobt_utc",
                            "to": "date",
                            "onError": None,
                            "onNull": None,
                        }
                    }
                }
            },
            {"$match": {"iobt": {"$ne": None}}},
            {
                "$group": {
                    "_id": {
                        "$dateTrunc": {
                            "date": "$iobt",
                            "unit": granularity,
                            "timezone": "UTC",
                        }
                    },
                    "flight_count": {"$sum": 1},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "period_start": "$_id",
                    "flight_count": 1,
                }
            },
        ]

        cursor = await self.collection.aggregate(pipeline)
        return await cursor.to_list(length=None)

    async def get_airport_distributions(
        self,
        *,
        batch: str,
        limit: int = 10,
    ) -> dict[str, list[dict[str, Any]]]:
        """返回批次内有界的起飞与到达机场分布。"""
        normalized_batch = batch.strip()
        if not normalized_batch:
            raise ValueError("batch不能为空")
        safe_limit = self._safe_limit(limit, default=10, maximum=50)

        def facet(field: str) -> list[dict[str, Any]]:
            return [
                {"$match": {field: {"$type": "string"}}},
                {"$group": {"_id": f"${field}", "flight_count": {"$sum": 1}}},
                {"$sort": {"flight_count": -1, "_id": 1}},
                {"$limit": safe_limit},
                {
                    "$project": {
                        "_id": 0,
                        "airport": "$_id",
                        "flight_count": 1,
                    }
                },
            ]

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                    "source.batch": normalized_batch,
                }
            },
            {
                "$facet": {
                    "departures": facet("identity.adep"),
                    "arrivals": facet("identity.ades"),
                }
            },
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        if documents:
            return documents[0]
        return {"departures": [], "arrivals": []}

    async def get_batch_data_quality(
        self,
        *,
        batch: str,
    ) -> dict[str, int]:
        """统计批次内关键身份、轨迹和预测字段的缺失数量。"""
        normalized_batch = batch.strip()
        if not normalized_batch:
            raise ValueError("batch不能为空")

        def missing_text(field: str) -> dict[str, Any]:
            return {
                "$eq": [
                    {
                        "$strLenCP": {
                            "$trim": {
                                "input": {
                                    "$convert": {
                                        "input": field,
                                        "to": "string",
                                        "onError": "",
                                        "onNull": "",
                                    }
                                }
                            }
                        }
                    },
                    0,
                ]
            }

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                    "source.batch": normalized_batch,
                }
            },
            {
                "$project": {
                    "missing_callsign": missing_text("$identity.callsign"),
                    "missing_aircraft_type": missing_text("$identity.aircraft_type"),
                    "missing_adep": missing_text("$identity.adep"),
                    "missing_ades": missing_text("$identity.ades"),
                    "missing_iobt": {
                        "$eq": [
                            {"$ifNull": ["$identity.iobt_utc", None]},
                            None,
                        ]
                    },
                    "empty_trajectory": {
                        "$eq": [
                            {"$size": {"$ifNull": ["$plots", []]}},
                            0,
                        ]
                    },
                    "without_prediction": {
                        "$eq": [
                            {"$size": {"$ifNull": ["$predicted_trajectory", []]}},
                            0,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "missing_callsign_count": {"$sum": {"$cond": ["$missing_callsign", 1, 0]}},
                    "missing_aircraft_type_count": {
                        "$sum": {"$cond": ["$missing_aircraft_type", 1, 0]}
                    },
                    "missing_departure_airport_count": {"$sum": {"$cond": ["$missing_adep", 1, 0]}},
                    "missing_arrival_airport_count": {"$sum": {"$cond": ["$missing_ades", 1, 0]}},
                    "missing_iobt_count": {"$sum": {"$cond": ["$missing_iobt", 1, 0]}},
                    "empty_trajectory_count": {"$sum": {"$cond": ["$empty_trajectory", 1, 0]}},
                    "flights_without_prediction_count": {
                        "$sum": {"$cond": ["$without_prediction", 1, 0]}
                    },
                }
            },
            {"$project": {"_id": 0}},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        if documents:
            return documents[0]
        return {
            "missing_callsign_count": 0,
            "missing_aircraft_type_count": 0,
            "missing_departure_airport_count": 0,
            "missing_arrival_airport_count": 0,
            "missing_iobt_count": 0,
            "empty_trajectory_count": 0,
            "flights_without_prediction_count": 0,
        }

    # =========================================================
    # GBAR v35 全库质量聚合表达式
    # =========================================================

    @staticmethod
    def _quality_haversine(
        lat_a: str,
        lon_a: str,
        lat_b: str,
        lon_b: str,
    ) -> dict[str, Any]:
        """两个轨迹点之间的球面距离（米）；参数是 Mongo 表达式字符串。"""
        return {
            "$multiply": [
                2.0,
                _GBAR_QUALITY_EARTH_RADIUS_METRES,
                {
                    "$asin": {
                        "$sqrt": {
                            "$add": [
                                {
                                    "$pow": [
                                        {
                                            "$sin": {
                                                "$divide": [
                                                    {
                                                        "$degreesToRadians": {
                                                            "$subtract": [lat_b, lat_a]
                                                        }
                                                    },
                                                    2.0,
                                                ]
                                            }
                                        },
                                        2.0,
                                    ]
                                },
                                {
                                    "$multiply": [
                                        {"$cos": {"$degreesToRadians": lat_a}},
                                        {"$cos": {"$degreesToRadians": lat_b}},
                                        {
                                            "$pow": [
                                                {
                                                    "$sin": {
                                                        "$divide": [
                                                            {
                                                                "$degreesToRadians": {
                                                                    "$subtract": [lon_b, lon_a]
                                                                }
                                                            },
                                                            2.0,
                                                        ]
                                                    }
                                                },
                                                2.0,
                                            ]
                                        },
                                    ]
                                },
                            ]
                        }
                    }
                },
            ]
        }

    @staticmethod
    def _quality_finite_numbers(*values: str) -> dict[str, Any]:
        """Guard arithmetic branches from null, strings, objects, NaN and Inf."""
        return {
            "$and": [
                condition
                for value in values
                for condition in (
                    {"$isNumber": value},
                    {"$eq": [value, value]},
                    {"$gte": [value, -1.7976931348623157e308]},
                    {"$lte": [value, 1.7976931348623157e308]},
                )
            ]
        }

    @staticmethod
    def _quality_contiguous_times(
        previous_time: str,
        current_time: str,
        following_time: str,
    ) -> dict[str, Any]:
        """Match the preprocessor's positive, at-most-15-second segment gaps."""
        maximum_gap_ms = SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS * 1_000.0
        return {
            "$and": [
                {"$ne": [previous_time, None]},
                {"$ne": [current_time, None]},
                {"$ne": [following_time, None]},
                {"$gt": [{"$subtract": [current_time, previous_time]}, 0]},
                {
                    "$lte": [
                        {"$subtract": [current_time, previous_time]},
                        maximum_gap_ms,
                    ]
                },
                {"$gt": [{"$subtract": [following_time, current_time]}, 0]},
                {
                    "$lte": [
                        {"$subtract": [following_time, current_time]},
                        maximum_gap_ms,
                    ]
                },
            ]
        }

    @staticmethod
    def _quality_time_array(plots: dict[str, Any]) -> dict[str, Any]:
        """Convert each timestamp once so all six feature scans can reuse it."""
        return {
            "$map": {
                "input": plots,
                "as": "quality_plot",
                "in": {
                    "$convert": {
                        "input": "$$quality_plot.time_of_track",
                        "to": "date",
                        "onError": None,
                        "onNull": None,
                    }
                },
            }
        }

    @classmethod
    def _quality_spike_middle_cond(
        cls,
        plots: dict[str, Any],
        times: dict[str, Any],
        index: str,
        *,
        path: str | None,
        threshold: float | None,
    ) -> dict[str, Any]:
        """三点孤立跳变中间点判定：path 为 None 时按 haversine 4 km 位置跳变。"""
        variables = {
            "cur": {"$arrayElemAt": [plots, index]},
            "prev": {"$arrayElemAt": [plots, {"$subtract": [index, 1]}]},
            "next": {"$arrayElemAt": [plots, {"$add": [index, 1]}]},
            "cur_time": {"$arrayElemAt": [times, index]},
            "prev_time": {"$arrayElemAt": [times, {"$subtract": [index, 1]}]},
            "next_time": {"$arrayElemAt": [times, {"$add": [index, 1]}]},
        }
        if path is not None:
            cur = f"$$cur.{path}"
            prev = f"$$prev.{path}"
            next_ = f"$$next.{path}"
            spec = next(item for item in _GBAR_QUALITY_FEATURES.values() if item["path"] == path)
            valid = {
                "$and": [
                    cls._quality_finite_numbers(cur, prev, next_),
                    cls._quality_contiguous_times("$$prev_time", "$$cur_time", "$$next_time"),
                    *(
                        {
                            "$and": [
                                {"$gte": [value, spec["minimum"]]},
                                {"$lte": [value, spec["maximum"]]},
                            ]
                        }
                        for value in (cur, prev, next_)
                    ),
                ]
            }
            rule = {
                "$and": [
                    {
                        "$gt": [
                            {
                                "$abs": {
                                    "$subtract": [
                                        cur,
                                        {"$divide": [{"$add": [prev, next_]}, 2.0]},
                                    ]
                                }
                            },
                            threshold,
                        ]
                    },
                    {
                        "$lte": [
                            {"$abs": {"$subtract": [next_, prev]}},
                            threshold,
                        ]
                    },
                ]
            }
        else:
            lat_path = "I062/105.lat"
            lon_path = "I062/105.lon"
            cur_lat = f"$$cur.{lat_path}"
            cur_lon = f"$$cur.{lon_path}"
            prev_lat = f"$$prev.{lat_path}"
            prev_lon = f"$$prev.{lon_path}"
            next_lat = f"$$next.{lat_path}"
            next_lon = f"$$next.{lon_path}"
            coordinates = (cur_lat, cur_lon, prev_lat, prev_lon, next_lat, next_lon)
            lat_spec = _GBAR_QUALITY_FEATURES["lat"]
            lon_spec = _GBAR_QUALITY_FEATURES["lon"]
            valid = {
                "$and": [
                    cls._quality_finite_numbers(*coordinates),
                    cls._quality_contiguous_times("$$prev_time", "$$cur_time", "$$next_time"),
                    *(
                        {
                            "$and": [
                                {"$gte": [value, spec["minimum"]]},
                                {"$lte": [value, spec["maximum"]]},
                            ]
                        }
                        for value, spec in (
                            (cur_lat, lat_spec),
                            (prev_lat, lat_spec),
                            (next_lat, lat_spec),
                            (cur_lon, lon_spec),
                            (prev_lon, lon_spec),
                            (next_lon, lon_spec),
                        )
                    ),
                ]
            }
            skip = _GBAR_QUALITY_SPIKE_SKIP_DEGREES
            metres = _GBAR_QUALITY_POSITION_SPIKE_METRES
            rule = {
                "$cond": [
                    {
                        "$and": [
                            {
                                "$lt": [
                                    {"$abs": {"$subtract": [cur_lat, prev_lat]}},
                                    skip,
                                ]
                            },
                            {
                                "$lt": [
                                    {"$abs": {"$subtract": [cur_lon, prev_lon]}},
                                    skip,
                                ]
                            },
                            {
                                "$lt": [
                                    {"$abs": {"$subtract": [next_lat, cur_lat]}},
                                    skip,
                                ]
                            },
                            {
                                "$lt": [
                                    {"$abs": {"$subtract": [next_lon, cur_lon]}},
                                    skip,
                                ]
                            },
                        ]
                    },
                    False,
                    {
                        "$and": [
                            {
                                "$gt": [
                                    cls._quality_haversine(prev_lat, prev_lon, cur_lat, cur_lon),
                                    metres,
                                ]
                            },
                            {
                                "$gt": [
                                    cls._quality_haversine(cur_lat, cur_lon, next_lat, next_lon),
                                    metres,
                                ]
                            },
                            {
                                "$lte": [
                                    cls._quality_haversine(prev_lat, prev_lon, next_lat, next_lon),
                                    metres,
                                ]
                            },
                        ]
                    },
                ]
            }
        return {
            "$let": {
                "vars": variables,
                "in": {"$cond": [valid, rule, False]},
            }
        }

    @classmethod
    def _quality_spike_tail_cond(
        cls,
        plots: dict[str, Any],
        times: dict[str, Any],
        *,
        path: str | None,
        threshold: float | None,
    ) -> dict[str, Any]:
        """末尾点孤立跳变判定（需要最后三个点）。"""
        variables = {
            "last": {"$arrayElemAt": [plots, -1]},
            "prev": {"$arrayElemAt": [plots, -2]},
            "prev2": {"$arrayElemAt": [plots, -3]},
            "last_time": {"$arrayElemAt": [times, -1]},
            "prev_time": {"$arrayElemAt": [times, -2]},
            "prev2_time": {"$arrayElemAt": [times, -3]},
        }
        if path is not None:
            last = f"$$last.{path}"
            prev = f"$$prev.{path}"
            prev2 = f"$$prev2.{path}"
            spec = next(item for item in _GBAR_QUALITY_FEATURES.values() if item["path"] == path)
            valid = {
                "$and": [
                    cls._quality_finite_numbers(last, prev, prev2),
                    cls._quality_contiguous_times("$$prev2_time", "$$prev_time", "$$last_time"),
                    *(
                        {
                            "$and": [
                                {"$gte": [value, spec["minimum"]]},
                                {"$lte": [value, spec["maximum"]]},
                            ]
                        }
                        for value in (last, prev, prev2)
                    ),
                ]
            }
            rule: dict[str, Any] = {
                "$and": [
                    {
                        "$gt": [
                            {"$abs": {"$subtract": [last, prev]}},
                            threshold,
                        ]
                    },
                    {
                        "$lte": [
                            {"$abs": {"$subtract": [prev, prev2]}},
                            threshold,
                        ]
                    },
                ]
            }
        else:
            lat_path = "I062/105.lat"
            lon_path = "I062/105.lon"
            last_lat = f"$$last.{lat_path}"
            last_lon = f"$$last.{lon_path}"
            prev_lat = f"$$prev.{lat_path}"
            prev_lon = f"$$prev.{lon_path}"
            prev2_lat = f"$$prev2.{lat_path}"
            prev2_lon = f"$$prev2.{lon_path}"
            skip = _GBAR_QUALITY_SPIKE_SKIP_DEGREES
            metres = _GBAR_QUALITY_POSITION_SPIKE_METRES
            coordinates = (
                last_lat,
                last_lon,
                prev_lat,
                prev_lon,
                prev2_lat,
                prev2_lon,
            )
            lat_spec = _GBAR_QUALITY_FEATURES["lat"]
            lon_spec = _GBAR_QUALITY_FEATURES["lon"]
            valid = {
                "$and": [
                    cls._quality_finite_numbers(*coordinates),
                    cls._quality_contiguous_times("$$prev2_time", "$$prev_time", "$$last_time"),
                    *(
                        {
                            "$and": [
                                {"$gte": [value, spec["minimum"]]},
                                {"$lte": [value, spec["maximum"]]},
                            ]
                        }
                        for value, spec in (
                            (last_lat, lat_spec),
                            (prev_lat, lat_spec),
                            (prev2_lat, lat_spec),
                            (last_lon, lon_spec),
                            (prev_lon, lon_spec),
                            (prev2_lon, lon_spec),
                        )
                    ),
                ]
            }
            rule = {
                "$cond": [
                    {
                        "$and": [
                            {
                                "$lt": [
                                    {"$abs": {"$subtract": [last_lat, prev_lat]}},
                                    skip,
                                ]
                            },
                            {
                                "$lt": [
                                    {"$abs": {"$subtract": [last_lon, prev_lon]}},
                                    skip,
                                ]
                            },
                        ]
                    },
                    False,
                    {
                        "$and": [
                            {
                                "$gt": [
                                    cls._quality_haversine(last_lat, last_lon, prev_lat, prev_lon),
                                    metres,
                                ]
                            },
                            {
                                "$lte": [
                                    cls._quality_haversine(
                                        prev_lat, prev_lon, prev2_lat, prev2_lon
                                    ),
                                    metres,
                                ]
                            },
                        ]
                    },
                ]
            }
        return {
            "$cond": [
                {"$gte": [{"$size": plots}, 3]},
                {
                    "$let": {
                        "vars": variables,
                        "in": {"$cond": [valid, rule, False]},
                    }
                },
                False,
            ]
        }

    @classmethod
    def _quality_spike_count(
        cls,
        plots: dict[str, Any],
        times: dict[str, Any],
        *,
        path: str | None,
        threshold: float | None,
    ) -> dict[str, Any]:
        """一个特征（或位置对）的跳变计数 = 中间点 $filter 计数 + 尾部判定。"""
        return {
            "$add": [
                {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$range": [
                                    1,
                                    {"$max": [1, {"$subtract": [{"$size": plots}, 1]}]},
                                ]
                            },
                            "as": "i",
                            "cond": cls._quality_spike_middle_cond(
                                plots,
                                times,
                                "$$i",
                                path=path,
                                threshold=threshold,
                            ),
                        }
                    }
                },
                {
                    "$cond": [
                        cls._quality_spike_tail_cond(plots, times, path=path, threshold=threshold),
                        1,
                        0,
                    ]
                },
            ]
        }

    @classmethod
    def _quality_per_flight_projection(
        cls,
        plots: dict[str, Any],
        times: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """单航班质量投影（两阶段，零输出分配）。

        第一阶段用 $size($filter(...)) 计算 12 个逐特征缺失/越界计数、
        2 个点级并集计数与 6 个跳变计数；第二阶段按航班级加总
        spike_value_count（每航班一次，成本可忽略）。
        """

        def filter_size(cond: dict[str, Any]) -> dict[str, Any]:
            return {"$size": {"$filter": {"input": plots, "as": "plot", "cond": cond}}}

        fields: dict[str, Any] = {
            "batch": "$source.batch",
            "external_id": "$source.external_id",
            "original_filename": "$ingest.original_filename",
            "callsign": "$identity.callsign",
            "point_count": {"$size": plots},
        }
        missing_conds: dict[str, dict[str, Any]] = {}
        outlier_conds: dict[str, dict[str, Any]] = {}
        for name, spec in _GBAR_QUALITY_FEATURES.items():
            path = spec["path"]
            value = f"$$plot.{path}"
            missing_conds[name] = {
                "$or": [
                    {"$not": [{"$isNumber": value}]},
                    {"$and": [{"$isNumber": value}, {"$ne": [value, value]}]},
                ]
            }
            outlier_conds[name] = {
                "$and": [
                    {"$isNumber": value},
                    {"$eq": [value, value]},
                    {
                        "$or": [
                            {"$lt": [value, spec["minimum"]]},
                            {"$gt": [value, spec["maximum"]]},
                        ]
                    },
                ]
            }
        for name in _GBAR_QUALITY_FEATURES:
            fields[f"missing_{name}_count"] = filter_size(missing_conds[name])
            fields[f"outlier_{name}_count"] = filter_size(outlier_conds[name])
        fields["missing_point_count"] = filter_size({"$or": list(missing_conds.values())})
        fields["outlier_point_count"] = filter_size({"$or": list(outlier_conds.values())})
        for name, spec in _GBAR_QUALITY_FEATURES.items():
            fields[f"spike_{name}_count"] = cls._quality_spike_count(
                plots,
                times,
                path=None if name in ("lat", "lon") else spec["path"],
                threshold=None if name in ("lat", "lon") else spec["spike_threshold"],
            )
        stage1: dict[str, Any] = {"$project": fields}
        stage2: dict[str, Any] = {
            "$addFields": {
                "spike_value_count": {
                    "$add": [f"$spike_{name}_count" for name in _GBAR_QUALITY_FEATURES]
                }
            }
        }
        return [stage1, stage2]

    @classmethod
    def _quality_expressions(
        cls,
        plots: dict[str, Any],
        times: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """构造缺失、范围越界和相邻点跳变三组逐特征聚合表达式（供测试与旧调用使用）。"""
        missing: dict[str, dict[str, Any]] = {}
        outlier: dict[str, dict[str, Any]] = {}
        spike: dict[str, dict[str, Any]] = {}
        for name, spec in _GBAR_QUALITY_FEATURES.items():
            path = spec["path"]
            value = f"$$plot.{path}"
            missing[f"missing_{name}"] = {
                "$or": [
                    {"$not": [{"$isNumber": value}]},
                    {"$and": [{"$isNumber": value}, {"$ne": [value, value]}]},
                ]
            }
            outlier[f"outlier_{name}"] = {
                "$and": [
                    {"$isNumber": value},
                    {"$eq": [value, value]},
                    {
                        "$or": [
                            {"$lt": [value, spec["minimum"]]},
                            {"$gt": [value, spec["maximum"]]},
                        ]
                    },
                ]
            }
            spike[f"spike_{name}"] = cls._quality_spike_count(
                plots,
                times,
                path=None if name in ("lat", "lon") else path,
                threshold=None if name in ("lat", "lon") else spec["spike_threshold"],
            )
        return missing, outlier, spike

    @staticmethod
    def _quality_problem_condition() -> dict[str, Any]:
        """“问题航班”判定：空轨迹、缺失、越界或跳变任一项命中。"""
        return {
            "$or": [
                {"$eq": ["$point_count", 0]},
                {"$gt": ["$missing_point_count", 0]},
                {"$gt": ["$outlier_point_count", 0]},
                {"$gt": ["$spike_value_count", 0]},
            ]
        }

    @staticmethod
    def _quality_clean_condition() -> dict[str, Any]:
        """非空且在当前质量策略下没有缺失、越界或跳变。"""
        return {
            "$and": [
                {"$gt": ["$point_count", 0]},
                {"$eq": ["$missing_point_count", 0]},
                {"$eq": ["$outlier_point_count", 0]},
                {"$eq": ["$spike_value_count", 0]},
            ]
        }

    @staticmethod
    def _quality_match(batch: str | None) -> dict[str, Any]:
        match: dict[str, Any] = {"source.system": SCAT_SOURCE_SYSTEM}
        if batch:
            match["source.batch"] = batch
        return match

    @staticmethod
    def _quality_partition_matches(
        match: dict[str, Any],
        partitions: int,
    ) -> list[dict[str, Any]]:
        """Partition numeric ids and retain non-numeric/missing ids in shard zero."""
        matches: list[dict[str, Any]] = []
        for index in range(partitions):
            numeric_partition = {"id": {"$mod": [partitions, index]}}
            if index == 0:
                matches.append(
                    {
                        "$and": [
                            match,
                            {
                                "$or": [
                                    numeric_partition,
                                    {"id": {"$not": {"$type": "number"}}},
                                ]
                            },
                        ]
                    }
                )
            else:
                matches.append({**match, **numeric_partition})
        return matches

    async def get_model_data_quality_summary(
        self,
        *,
        batch: str | None = None,
        include_flight_rows: bool = False,
    ) -> dict[str, Any]:
        """Aggregate missing, range and adjacent-point spike problems across SCAT flights.

        This is intentionally one MongoDB aggregation, not 13k calls to the
        single-flight quality tool.  Spike detection mirrors the GBAR v35
        preprocessing rules (isolated 3-point jumps, converted thresholds),
        but only reports counts; repair decisions stay in the per-flight
        preprocessing layer. Optional compact per-flight rows are reserved for
        the service's shared short-lived cache and must not be sent to an Agent.
        """
        normalized_batch = batch.strip() if batch else None
        if batch is not None and not normalized_batch:
            raise ValueError("batch不能为空")

        plots = {"$ifNull": ["$plots", []]}
        times = {"$ifNull": ["$_quality_times", []]}
        per_flight_projection = self._quality_per_flight_projection(plots, times)
        missing_names = [f"missing_{name}" for name in _GBAR_QUALITY_FEATURES]
        outlier_names = [f"outlier_{name}" for name in _GBAR_QUALITY_FEATURES]
        spike_names = [f"spike_{name}" for name in _GBAR_QUALITY_FEATURES]
        match = self._quality_match(normalized_batch)
        problem_condition = self._quality_problem_condition()
        clean_condition = self._quality_clean_condition()

        summary_group: dict[str, Any] = {
            "_id": None,
            "flight_count": {"$sum": 1},
            "point_count": {"$sum": "$point_count"},
            "empty_trajectory_count": {"$sum": {"$cond": [{"$eq": ["$point_count", 0]}, 1, 0]}},
            "flights_with_missing": {
                "$sum": {
                    "$cond": [
                        {
                            "$or": [
                                {"$eq": ["$point_count", 0]},
                                {"$gt": ["$missing_point_count", 0]},
                            ]
                        },
                        1,
                        0,
                    ]
                }
            },
            "flights_with_outliers": {
                "$sum": {"$cond": [{"$gt": ["$outlier_point_count", 0]}, 1, 0]}
            },
            "flights_with_spikes": {"$sum": {"$cond": [{"$gt": ["$spike_value_count", 0]}, 1, 0]}},
            "flights_with_missing_and_outliers": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$gt": ["$missing_point_count", 0]},
                                {"$gt": ["$outlier_point_count", 0]},
                            ]
                        },
                        1,
                        0,
                    ]
                }
            },
            "flights_with_any_problem": {"$sum": {"$cond": [problem_condition, 1, 0]}},
            "clean_flight_count": {"$sum": {"$cond": [clean_condition, 1, 0]}},
            "missing_point_count": {"$sum": "$missing_point_count"},
            "outlier_point_count": {"$sum": "$outlier_point_count"},
            "spike_value_count": {"$sum": "$spike_value_count"},
        }
        for name in missing_names + outlier_names + spike_names:
            summary_group[f"{name}_count"] = {"$sum": f"${name}_count"}

        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {"$set": {"_quality_times": self._quality_time_array(plots)}},
            *per_flight_projection,
            {
                "$facet": {
                    "summary": [
                        {"$group": summary_group},
                        {"$project": {"_id": 0}},
                    ],
                    "batches": [
                        {
                            "$group": {
                                "_id": "$batch",
                                "flight_count": {"$sum": 1},
                                "flights_with_any_problem": {
                                    "$sum": {"$cond": [problem_condition, 1, 0]}
                                },
                                "clean_flight_count": {"$sum": {"$cond": [clean_condition, 1, 0]}},
                            }
                        },
                        {"$sort": {"_id": 1}},
                        {
                            "$project": {
                                "_id": 0,
                                "batch": "$_id",
                                "flight_count": 1,
                                "flights_with_any_problem": 1,
                                "clean_flight_count": 1,
                            }
                        },
                    ],
                    "clean_samples": [
                        {"$match": {"$expr": clean_condition}},
                        {"$sort": {"batch": 1, "external_id": 1}},
                        {"$limit": _GBAR_QUALITY_CLEAN_SAMPLE_LIMIT},
                        {
                            "$project": {
                                "_id": 0,
                                "batch": 1,
                                "external_id": 1,
                                "original_filename": 1,
                                "callsign": 1,
                                "point_count": 1,
                            }
                        },
                    ],
                }
            },
        ]
        if include_flight_rows:
            pipeline[-1]["$facet"]["flight_rows"] = [
                {
                    "$project": {
                        "_id": 0,
                        "batch": 1,
                        "external_id": 1,
                        "original_filename": 1,
                        "callsign": 1,
                        "point_count": 1,
                        "missing_point_count": 1,
                        "outlier_point_count": 1,
                        "spike_value_count": 1,
                    }
                }
            ]

        async def run_partition(partition_match: dict[str, Any]) -> dict[str, Any]:
            partition_pipeline = [
                {"$match": partition_match},
                *pipeline[1:],
            ]
            cursor = await self.analytics_collection.aggregate(
                partition_pipeline,
                allowDiskUse=True,
                maxTimeMS=180_000,
            )
            documents = await cursor.to_list(length=1)
            return (
                documents[0]
                if documents
                else {"summary": [], "batches": [], "clean_samples": [], "flight_rows": []}
            )

        partitions = _GBAR_QUALITY_PARTITIONS
        partition_matches = self._quality_partition_matches(match, partitions)
        expected_flight_count = await self.count_all(batch=normalized_batch)
        async with _GBAR_QUALITY_SCAN_SEMAPHORE:
            partition_results = await asyncio.gather(
                *(run_partition(partition_match) for partition_match in partition_matches)
            )

        merged_summary: dict[str, Any] = {}
        for partition_result in partition_results:
            part = (partition_result.get("summary") or [{}])[0]
            for key, value in part.items():
                merged_summary[key] = merged_summary.get(key, 0) + int(value or 0)

        batch_rows: dict[str, dict[str, Any]] = {}
        for partition_result in partition_results:
            for row in partition_result.get("batches") or []:
                batch_name = row.get("batch")
                target = batch_rows.setdefault(
                    batch_name,
                    {
                        "batch": batch_name,
                        "flight_count": 0,
                        "flights_with_any_problem": 0,
                        "clean_flight_count": 0,
                    },
                )
                target["flight_count"] += int(row.get("flight_count") or 0)
                target["flights_with_any_problem"] += int(row.get("flights_with_any_problem") or 0)
                target["clean_flight_count"] += int(row.get("clean_flight_count") or 0)

        clean_samples: list[dict[str, Any]] = []
        for partition_result in partition_results:
            clean_samples.extend(partition_result.get("clean_samples") or [])
        clean_samples.sort(
            key=lambda row: (
                str(row.get("batch") or ""),
                str(row.get("external_id") or ""),
            )
        )
        clean_samples = clean_samples[:_GBAR_QUALITY_CLEAN_SAMPLE_LIMIT]
        for row in clean_samples:
            row["original_filename"] = row.get("original_filename") or None
            row["trajectory_key"] = f"{row.get('batch') or ''}/{row.get('external_id') or ''}"

        flight_rows: list[dict[str, Any]] = []
        if include_flight_rows:
            for partition_result in partition_results:
                flight_rows.extend(partition_result.get("flight_rows") or [])
            flight_rows.sort(
                key=lambda row: (
                    str(row.get("batch") or ""),
                    str(row.get("external_id") or ""),
                )
            )
            for row in flight_rows:
                row["original_filename"] = row.get("original_filename") or None
                row["trajectory_key"] = f"{row.get('batch') or ''}/{row.get('external_id') or ''}"
            if len(flight_rows) != expected_flight_count:
                raise RuntimeError(
                    "SCAT quality snapshot row coverage mismatch: "
                    f"expected {expected_flight_count}, got {len(flight_rows)}"
                )

        result = {
            "summary": [merged_summary],
            "batches": sorted(batch_rows.values(), key=lambda row: str(row.get("batch") or "")),
            "clean_samples": clean_samples,
        }
        summary = (result.get("summary") or [{}])[0]
        if int(summary.get("flight_count") or 0) != expected_flight_count:
            raise RuntimeError(
                "SCAT quality scan partition coverage mismatch: "
                f"expected {expected_flight_count}, got "
                f"{int(summary.get('flight_count') or 0)}"
            )
        classified_flight_count = int(summary.get("flights_with_any_problem") or 0) + int(
            summary.get("clean_flight_count") or 0
        )
        if classified_flight_count != expected_flight_count:
            raise RuntimeError(
                "SCAT quality scan classification mismatch: "
                f"expected {expected_flight_count}, got {classified_flight_count}"
            )
        features = []
        for name in _GBAR_QUALITY_FEATURES:
            missing_count = int(summary.pop(f"missing_{name}_count", 0))
            outlier_count = int(summary.pop(f"outlier_{name}_count", 0))
            spike_count = int(summary.pop(f"spike_{name}_count", 0))
            features.append(
                {
                    "feature": name,
                    "missing_or_non_finite_value_count": missing_count,
                    "out_of_range_value_count": outlier_count,
                    "adjacent_point_spike_count": spike_count,
                }
            )
        summary["missing_value_count"] = sum(
            item["missing_or_non_finite_value_count"] for item in features
        )
        summary["outlier_value_count"] = sum(item["out_of_range_value_count"] for item in features)
        summary["spike_value_count"] = sum(item["adjacent_point_spike_count"] for item in features)
        for key in (
            "flight_count",
            "point_count",
            "empty_trajectory_count",
            "flights_with_missing",
            "flights_with_outliers",
            "flights_with_spikes",
            "flights_with_missing_and_outliers",
            "flights_with_any_problem",
            "clean_flight_count",
            "missing_point_count",
            "outlier_point_count",
            "spike_value_count",
        ):
            summary.setdefault(key, 0)
        response = {
            "scope": "all_scat_flights" if normalized_batch is None else "batch",
            "batch": normalized_batch,
            "summary": summary,
            "features": features,
            "batches": result.get("batches") or [],
            "clean_trajectory_samples": result.get("clean_samples") or [],
            "clean_definition": (
                "non-empty trajectory with zero missing/non-finite, physical-range "
                "violation and isolated adjacent-point spike counts under this policy"
            ),
            "identifier_note": (
                "Use original_filename when present; batch + external_id is the "
                "authoritative SCAT trajectory identifier."
            ),
            "anomaly_definition": (
                "GBAR v35 physical-range violation or isolated "
                "adjacent-point spike (3-point rule, haversine 4 km "
                "position / converted scalar thresholds; finite in-range "
                "samples with consecutive gaps no greater than 15 seconds)"
            ),
            "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
            "partition_coverage_verified": True,
            "trajectory_ordering": "stored_array_order_monotonic_triples_only",
            "temporal_spikes_included": True,
        }
        if include_flight_rows:
            response["_quality_flight_rows"] = flight_rows
        return response

    async def list_model_data_quality_problem_flights(
        self,
        *,
        batch: str | None = None,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "outlier_point_count",
    ) -> dict[str, Any]:
        """分页列出包含缺失、越界或跳变点的问题航班。

        与全库统计使用同一套判定表达式；默认按越界点数降序，每页
        最多 100 条（Agent 默认 20 条）。
        """
        normalized_batch = batch.strip() if batch else None
        if batch is not None and not normalized_batch:
            raise ValueError("batch不能为空")
        if sort_by not in _GBAR_QUALITY_SORT_FIELDS:
            raise ValueError("sort_by 必须是 " + ", ".join(sorted(_GBAR_QUALITY_SORT_FIELDS)))
        safe_limit = self._safe_limit(limit, default=20, maximum=100)
        safe_skip = max(offset, 0)
        if sort_by == "external_id":
            sort_stage: dict[str, Any] = {"$sort": {"external_id": 1}}
        else:
            sort_stage = {"$sort": {sort_by: -1, "external_id": 1}}

        plots = {"$ifNull": ["$plots", []]}
        times = {"$ifNull": ["$_quality_times", []]}
        projection = self._quality_per_flight_projection(plots, times)
        pipeline: list[dict[str, Any]] = [
            {"$match": self._quality_match(normalized_batch)},
            {"$set": {"_quality_times": self._quality_time_array(plots)}},
            *projection,
            {"$match": {"$expr": self._quality_problem_condition()}},
            {
                "$facet": {
                    "total": [{"$count": "count"}],
                    "page": [
                        sort_stage,
                        {"$limit": safe_skip + safe_limit},
                        {
                            "$project": {
                                "_id": 0,
                                "batch": 1,
                                "external_id": 1,
                                "point_count": 1,
                                "missing_point_count": 1,
                                "outlier_point_count": 1,
                                "spike_value_count": 1,
                            }
                        },
                    ],
                }
            },
        ]

        async def run_partition(partition_match: dict[str, Any]) -> dict[str, Any]:
            partition_pipeline = [
                {"$match": partition_match},
                *pipeline[1:],
            ]
            cursor = await self.analytics_collection.aggregate(
                partition_pipeline,
                allowDiskUse=True,
                maxTimeMS=180_000,
            )
            documents = await cursor.to_list(length=1)
            return documents[0] if documents else {"total": [], "page": []}

        partitions = _GBAR_QUALITY_PARTITIONS
        partition_matches = self._quality_partition_matches(
            self._quality_match(normalized_batch), partitions
        )
        async with _GBAR_QUALITY_SCAN_SEMAPHORE:
            partition_results = await asyncio.gather(
                *(run_partition(partition_match) for partition_match in partition_matches)
            )

        total = 0
        for partition_result in partition_results:
            total_rows = partition_result.get("total") or []
            total += int(total_rows[0]["count"]) if total_rows else 0

        candidates: list[dict[str, Any]] = []
        for partition_result in partition_results:
            candidates.extend(partition_result.get("page") or [])

        if sort_by == "external_id":
            candidates.sort(key=lambda row: str(row.get("external_id") or ""))
        else:
            candidates.sort(
                key=lambda row: (
                    -(int(row.get(sort_by) or 0)),
                    str(row.get("external_id") or ""),
                )
            )
        items = candidates[safe_skip : safe_skip + safe_limit]
        return {
            "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
            "trajectory_ordering": "stored_array_order_monotonic_triples_only",
            "total": total,
            "offset": safe_skip,
            "limit": safe_limit,
            "returned": len(items),
            "has_more": safe_skip + len(items) < total,
            "items": items,
        }

    async def list_model_data_quality_clean_flights(
        self,
        *,
        batch: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页列出当前GBAR质量策略下没有缺失、越界或跳变的轨迹。"""
        normalized_batch = batch.strip() if batch else None
        if batch is not None and not normalized_batch:
            raise ValueError("batch不能为空")
        safe_limit = self._safe_limit(limit, default=20, maximum=100)
        safe_skip = max(offset, 0)

        plots = {"$ifNull": ["$plots", []]}
        times = {"$ifNull": ["$_quality_times", []]}
        projection = self._quality_per_flight_projection(plots, times)
        pipeline: list[dict[str, Any]] = [
            {"$match": self._quality_match(normalized_batch)},
            {"$set": {"_quality_times": self._quality_time_array(plots)}},
            *projection,
            {"$match": {"$expr": self._quality_clean_condition()}},
            {
                "$facet": {
                    "total": [{"$count": "count"}],
                    "page": [
                        {"$sort": {"batch": 1, "external_id": 1}},
                        {"$limit": safe_skip + safe_limit},
                        {
                            "$project": {
                                "_id": 0,
                                "batch": 1,
                                "external_id": 1,
                                "original_filename": 1,
                                "callsign": 1,
                                "point_count": 1,
                            }
                        },
                    ],
                }
            },
        ]

        async def run_partition(partition_match: dict[str, Any]) -> dict[str, Any]:
            partition_pipeline = [{"$match": partition_match}, *pipeline[1:]]
            cursor = await self.analytics_collection.aggregate(
                partition_pipeline,
                allowDiskUse=True,
                maxTimeMS=180_000,
            )
            documents = await cursor.to_list(length=1)
            return documents[0] if documents else {"total": [], "page": []}

        partition_matches = self._quality_partition_matches(
            self._quality_match(normalized_batch), _GBAR_QUALITY_PARTITIONS
        )
        async with _GBAR_QUALITY_SCAN_SEMAPHORE:
            partition_results = await asyncio.gather(
                *(run_partition(partition_match) for partition_match in partition_matches)
            )

        total = 0
        candidates: list[dict[str, Any]] = []
        for partition_result in partition_results:
            total_rows = partition_result.get("total") or []
            total += int(total_rows[0]["count"]) if total_rows else 0
            candidates.extend(partition_result.get("page") or [])

        candidates.sort(
            key=lambda row: (
                str(row.get("batch") or ""),
                str(row.get("external_id") or ""),
            )
        )
        items = candidates[safe_skip : safe_skip + safe_limit]
        for row in items:
            row["original_filename"] = row.get("original_filename") or None
            row["trajectory_key"] = f"{row.get('batch') or ''}/{row.get('external_id') or ''}"

        return {
            "quality_policy_version": SCAT_GBAR_QUALITY_POLICY_VERSION,
            "clean_definition": (
                "non-empty trajectory with zero missing/non-finite, physical-range "
                "violation and isolated adjacent-point spike counts under this policy"
            ),
            "identifier_note": (
                "Use original_filename when present; batch + external_id is the "
                "authoritative SCAT trajectory identifier."
            ),
            "total": total,
            "offset": safe_skip,
            "limit": safe_limit,
            "returned": len(items),
            "has_more": safe_skip + len(items) < total,
            "items": items,
        }

    async def search_by_iobt_range(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        batch: str | None = None,
        limit: int = 20,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """按照计划撤轮挡时间范围查询航班。

        时间范围采用左闭右开区间：
            start_time <= iobt_utc < end_time
        """
        if start_time >= end_time:
            raise ValueError("start_time必须早于end_time")

        filters: dict[str, Any] = {
            "source.system": SCAT_SOURCE_SYSTEM,
            "identity.iobt_utc": {
                "$gte": start_time,
                "$lt": end_time,
            },
        }

        if batch:
            filters["source.batch"] = batch.strip()

        safe_limit = self._safe_limit(limit)
        safe_skip = self._safe_skip(skip)

        cursor = (
            self.collection.find(
                filters,
                FLIGHT_SUMMARY_PROJECTION,
            )
            .sort("identity.iobt_utc", 1)
            .skip(safe_skip)
            .limit(safe_limit)
        )

        return await cursor.to_list(length=safe_limit)

    async def get_trajectory_summary(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any] | None:
        """统计一条航班轨迹的范围、速度、垂直状态和数据质量。"""
        ground_speed_expression: dict[str, Any] = {
            "$sqrt": {
                "$add": [
                    {"$pow": ["$vx", 2]},
                    {"$pow": ["$vy", 2]},
                ]
            }
        }

        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._source_filter(
                    batch=batch,
                    external_id=external_id,
                )
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "plots": {"$ifNull": ["$plots", []]},
                }
            },
            {"$unwind": "$plots"},
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "timestamp": "$plots.time_of_track",
                    "lat": "$plots.I062/105.lat",
                    "lon": "$plots.I062/105.lon",
                    "flight_level": ("$plots.I062/136.measured_flight_level"),
                    "vx": "$plots.I062/185.vx",
                    "vy": "$plots.I062/185.vy",
                    "vertical_mode": ("$plots.I062/200.vert"),
                    "rocd": "$plots.I062/220.rocd",
                    "mode_s": "$plots.I062/380",
                }
            },
            {
                "$group": {
                    "_id": None,
                    "id": {"$first": "$id"},
                    "source": {"$first": "$source"},
                    "identity": {"$first": "$identity"},
                    "point_count": {"$sum": 1},
                    "start_time": {"$min": "$timestamp"},
                    "end_time": {"$max": "$timestamp"},
                    "min_lat": {"$min": "$lat"},
                    "max_lat": {"$max": "$lat"},
                    "min_lon": {"$min": "$lon"},
                    "max_lon": {"$max": "$lon"},
                    "min_flight_level": {"$min": "$flight_level"},
                    "max_flight_level": {"$max": "$flight_level"},
                    "average_flight_level": {"$avg": "$flight_level"},
                    "average_ground_speed_mps": {
                        "$avg": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$isNumber": "$vx"},
                                        {"$isNumber": "$vy"},
                                    ]
                                },
                                ground_speed_expression,
                                None,
                            ]
                        }
                    },
                    "maximum_ground_speed_mps": {
                        "$max": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$isNumber": "$vx"},
                                        {"$isNumber": "$vy"},
                                    ]
                                },
                                ground_speed_expression,
                                None,
                            ]
                        }
                    },
                    "average_absolute_rocd": {
                        "$avg": {
                            "$cond": [
                                {"$isNumber": "$rocd"},
                                {"$abs": "$rocd"},
                                None,
                            ]
                        }
                    },
                    "maximum_absolute_rocd": {
                        "$max": {
                            "$cond": [
                                {"$isNumber": "$rocd"},
                                {"$abs": "$rocd"},
                                None,
                            ]
                        }
                    },
                    "vertical_modes": {"$push": "$vertical_mode"},
                    "timestamps": {"$push": "$timestamp"},
                    "missing_position_points": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$isNumber": "$lat"},
                                        {"$isNumber": "$lon"},
                                    ]
                                },
                                0,
                                1,
                            ]
                        }
                    },
                    "missing_motion_points": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$isNumber": "$flight_level"},
                                        {"$isNumber": "$vx"},
                                        {"$isNumber": "$vy"},
                                        {"$isNumber": "$rocd"},
                                        {"$isNumber": "$vertical_mode"},
                                    ]
                                },
                                0,
                                1,
                            ]
                        }
                    },
                    "mode_s_present_points": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$eq": [
                                        {"$type": "$mode_s"},
                                        "object",
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "point_count": 1,
                    "start_time": 1,
                    "end_time": 1,
                    "geographic_bounds": {
                        "min_lat": "$min_lat",
                        "max_lat": "$max_lat",
                        "min_lon": "$min_lon",
                        "max_lon": "$max_lon",
                    },
                    "flight_level": {
                        "minimum": "$min_flight_level",
                        "maximum": "$max_flight_level",
                        "average": {
                            "$round": [
                                "$average_flight_level",
                                2,
                            ]
                        },
                    },
                    "ground_speed_mps": {
                        "average": {
                            "$round": [
                                "$average_ground_speed_mps",
                                2,
                            ]
                        },
                        "maximum": {
                            "$round": [
                                "$maximum_ground_speed_mps",
                                2,
                            ]
                        },
                    },
                    "absolute_rocd": {
                        "average": {
                            "$round": [
                                "$average_absolute_rocd",
                                2,
                            ]
                        },
                        "maximum": {
                            "$round": [
                                "$maximum_absolute_rocd",
                                2,
                            ]
                        },
                    },
                    "vertical_mode_counts": {
                        "level": {
                            "$size": {
                                "$filter": {
                                    "input": "$vertical_modes",
                                    "as": "mode",
                                    "cond": {"$eq": ["$$mode", 0]},
                                }
                            }
                        },
                        "climb": {
                            "$size": {
                                "$filter": {
                                    "input": "$vertical_modes",
                                    "as": "mode",
                                    "cond": {"$eq": ["$$mode", 1]},
                                }
                            }
                        },
                        "descent": {
                            "$size": {
                                "$filter": {
                                    "input": "$vertical_modes",
                                    "as": "mode",
                                    "cond": {"$eq": ["$$mode", 2]},
                                }
                            }
                        },
                        "uncertain": {
                            "$size": {
                                "$filter": {
                                    "input": "$vertical_modes",
                                    "as": "mode",
                                    "cond": {"$eq": ["$$mode", 3]},
                                }
                            }
                        },
                    },
                    "data_quality": {
                        "missing_position_points": ("$missing_position_points"),
                        "missing_motion_points": ("$missing_motion_points"),
                        "mode_s_present_points": ("$mode_s_present_points"),
                        "mode_s_coverage_ratio": {
                            "$cond": [
                                {"$gt": ["$point_count", 0]},
                                {
                                    "$round": [
                                        {
                                            "$divide": [
                                                "$mode_s_present_points",
                                                "$point_count",
                                            ]
                                        },
                                        4,
                                    ]
                                },
                                0,
                            ]
                        },
                        "unique_timestamp_count": {
                            "$size": {
                                "$setUnion": [
                                    "$timestamps",
                                    [],
                                ]
                            }
                        },
                        "duplicate_timestamp_count": {
                            "$subtract": [
                                "$point_count",
                                {
                                    "$size": {
                                        "$setUnion": [
                                            "$timestamps",
                                            [],
                                        ]
                                    }
                                },
                            ]
                        },
                    },
                }
            },
            {"$limit": 1},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        return documents[0] if documents else None

    async def get_fpl_summary(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any] | None:
        """返回飞行计划、放行、起降和等待事件摘要。"""
        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._source_filter(
                    batch=batch,
                    external_id=external_id,
                )
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "base_events": {
                        "$ifNull": [
                            "$fpl.fpl_base",
                            [],
                        ]
                    },
                    "departure_events": {
                        "$ifNull": [
                            "$fpl.fpl_dep",
                            [],
                        ]
                    },
                    "arrival_events": {
                        "$ifNull": [
                            "$fpl.fpl_arr",
                            [],
                        ]
                    },
                    "clearance_events": {
                        "$ifNull": [
                            "$fpl.fpl_clearance",
                            [],
                        ]
                    },
                    "holding_events": {
                        "$ifNull": [
                            "$fpl.fpl_holding",
                            [],
                        ]
                    },
                    "plan_update_events": {
                        "$ifNull": [
                            "$fpl.fpl_plan_update",
                            [],
                        ]
                    },
                }
            },
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "event_counts": {
                        "base": {"$size": "$base_events"},
                        "departure": {"$size": "$departure_events"},
                        "arrival": {"$size": "$arrival_events"},
                        "clearance": {"$size": "$clearance_events"},
                        "holding": {"$size": "$holding_events"},
                        "plan_update": {"$size": "$plan_update_events"},
                    },
                    "latest_base": {
                        "$arrayElemAt": [
                            "$base_events",
                            -1,
                        ]
                    },
                    "latest_departure": {
                        "$arrayElemAt": [
                            "$departure_events",
                            -1,
                        ]
                    },
                    "latest_arrival": {
                        "$arrayElemAt": [
                            "$arrival_events",
                            -1,
                        ]
                    },
                    "latest_clearance": {
                        "$arrayElemAt": [
                            "$clearance_events",
                            -1,
                        ]
                    },
                    "latest_holding": {
                        "$arrayElemAt": [
                            "$holding_events",
                            -1,
                        ]
                    },
                    "latest_plan_update": {
                        "$arrayElemAt": [
                            "$plan_update_events",
                            -1,
                        ]
                    },
                    "had_holding": {
                        "$gt": [
                            {
                                "$size": {
                                    "$filter": {
                                        "input": "$holding_events",
                                        "as": "event",
                                        "cond": {
                                            "$or": [
                                                {
                                                    "$eq": [
                                                        "$$event.holding_stack_status",
                                                        "HOLD",
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$$event.holding_status",
                                                        "NO HOLD",
                                                    ]
                                                },
                                            ]
                                        },
                                    }
                                }
                            },
                            0,
                        ]
                    },
                    "had_approach_clearance": {
                        "$gt": [
                            {
                                "$size": {
                                    "$filter": {
                                        "input": "$arrival_events",
                                        "as": "event",
                                        "cond": {
                                            "$eq": [
                                                "$$event.approach_clearance",
                                                True,
                                            ]
                                        },
                                    }
                                }
                            },
                            0,
                        ]
                    },
                }
            },
            {"$limit": 1},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        return documents[0] if documents else None

    async def get_prediction_summary(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> dict[str, Any] | None:
        """统计SCAT原始预测轨迹快照，不返回完整route数组。"""
        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._source_filter(
                    batch=batch,
                    external_id=external_id,
                )
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "snapshots": {
                        "$ifNull": [
                            "$predicted_trajectory",
                            [],
                        ]
                    },
                }
            },
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "snapshot_count": {"$size": "$snapshots"},
                    "first_snapshot_time": {
                        "$arrayElemAt": [
                            "$snapshots.time_stamp",
                            0,
                        ]
                    },
                    "last_snapshot_time": {
                        "$arrayElemAt": [
                            "$snapshots.time_stamp",
                            -1,
                        ]
                    },
                    "route_lengths": {
                        "$map": {
                            "input": "$snapshots",
                            "as": "snapshot",
                            "in": {
                                "$size": {
                                    "$ifNull": [
                                        "$$snapshot.route",
                                        [],
                                    ]
                                }
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "snapshot_count": 1,
                    "first_snapshot_time": 1,
                    "last_snapshot_time": 1,
                    "minimum_route_points": {"$min": "$route_lengths"},
                    "maximum_route_points": {"$max": "$route_lengths"},
                    "average_route_points": {
                        "$round": [
                            {"$avg": "$route_lengths"},
                            2,
                        ]
                    },
                }
            },
            {"$limit": 1},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        return documents[0] if documents else None

    async def list_prediction_snapshots(
        self,
        *,
        batch: str,
        external_id: str | int,
        start: int = 0,
        limit: int = 20,
    ) -> dict[str, Any] | None:
        """分页返回预测快照元数据，不加载每个快照的完整 route。"""
        safe_start = self._safe_skip(start)
        safe_limit = self._safe_limit(limit, default=20, maximum=100)

        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "snapshots": {"$ifNull": ["$predicted_trajectory", []]},
                }
            },
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "total_snapshots": {"$size": "$snapshots"},
                    "snapshots": {
                        "$map": {
                            "input": {"$slice": ["$snapshots", safe_start, safe_limit]},
                            "as": "snapshot",
                            "in": {
                                "time_stamp": "$$snapshot.time_stamp",
                                "route_point_count": {
                                    "$size": {"$ifNull": ["$$snapshot.route", []]}
                                },
                            },
                        }
                    },
                }
            },
            {"$limit": 1},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        if not documents:
            return None

        result = documents[0]
        result["start"] = safe_start
        result["returned_snapshots"] = len(result.get("snapshots") or [])
        return result

    async def get_prediction_snapshot_at_or_before(
        self,
        *,
        batch: str,
        external_id: str | int,
        timestamp: str,
    ) -> dict[str, Any] | None:
        """获取指定时刻之前最近的SCAT预测轨迹快照。

        SCAT原始时间字符串采用统一ISO格式，因此可以进行字符串
        时间顺序比较。该方法假定predicted_trajectory按时间升序保存。
        """
        normalized_timestamp = timestamp.strip()

        if not normalized_timestamp:
            raise ValueError("timestamp不能为空")

        pipeline: list[dict[str, Any]] = [
            {
                "$match": self._source_filter(
                    batch=batch,
                    external_id=external_id,
                )
            },
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "matched_snapshots": {
                        "$filter": {
                            "input": {
                                "$ifNull": [
                                    "$predicted_trajectory",
                                    [],
                                ]
                            },
                            "as": "snapshot",
                            "cond": {
                                "$lte": [
                                    "$$snapshot.time_stamp",
                                    normalized_timestamp,
                                ]
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "snapshot": {
                        "$arrayElemAt": [
                            "$matched_snapshots",
                            -1,
                        ]
                    },
                }
            },
            {"$limit": 1},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)

        if not documents:
            return None

        result = documents[0]

        if result.get("snapshot") is None:
            return None

        return result

    async def get_aircraft_type_distribution(
        self,
        *,
        batch: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """统计SCAT航班的机型分布。

        参数：
            batch:
                可选的数据批次。指定后只统计该批次。
            limit:
                返回机型数量，最大100。

        返回示例：
            [
                {
                    "aircraft_type": "B738",
                    "flight_count": 1520
                },
                ...
            ]
        """
        match_filter: dict[str, Any] = {
            "source.system": SCAT_SOURCE_SYSTEM,
            "identity.aircraft_type": {
                "$type": "string",
            },
        }

        if batch is not None:
            normalized_batch = batch.strip()

            if not normalized_batch:
                raise ValueError("batch不能为空字符串")

            match_filter["source.batch"] = normalized_batch

        safe_limit = self._safe_limit(
            limit,
            default=20,
            maximum=100,
        )

        pipeline: list[dict[str, Any]] = [
            {
                "$match": match_filter,
            },
            {
                "$group": {
                    "_id": "$identity.aircraft_type",
                    "flight_count": {
                        "$sum": 1,
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "aircraft_type": "$_id",
                    "flight_count": 1,
                }
            },
            {
                "$sort": {
                    "flight_count": -1,
                    "aircraft_type": 1,
                }
            },
            {
                "$limit": safe_limit,
            },
        ]

        cursor = await self.collection.aggregate(pipeline)

        return await cursor.to_list(length=safe_limit)

    async def get_airport_statistics(
        self,
        *,
        batch: str,
        limit: int = 20,
    ) -> list[dict[str, Any]] | None:
        """统计一个批次内各起飞机场的航班量与起飞延误（ATD-IOBT）。

        延误阈值 15 分钟按行业内常见口径固定；无 ATD 的航班计入
        with_atd_count 之外，不参与延误均值。
        """
        normalized_batch = batch.strip()
        if not normalized_batch:
            raise ValueError("batch不能为空")
        safe_limit = self._safe_limit(limit, default=20, maximum=100)

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                    "source.batch": normalized_batch,
                }
            },
            {
                "$project": {
                    "adep": "$identity.adep",
                    "has_atd": {
                        "$cond": [
                            {"$eq": [{"$type": "$identity.atd_utc"}, "date"]},
                            1,
                            0,
                        ]
                    },
                    "delay_minutes": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": [{"$type": "$identity.iobt_utc"}, "date"]},
                                    {"$eq": [{"$type": "$identity.atd_utc"}, "date"]},
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$subtract": [
                                            "$identity.atd_utc",
                                            "$identity.iobt_utc",
                                        ]
                                    },
                                    60000,
                                ]
                            },
                            None,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": "$adep",
                    "flight_count": {"$sum": 1},
                    "with_atd_count": {"$sum": "$has_atd"},
                    "mean_delay_minutes": {"$avg": "$delay_minutes"},
                    "max_delay_minutes": {"$max": "$delay_minutes"},
                    "min_delay_minutes": {"$min": "$delay_minutes"},
                    "delayed_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$delay_minutes", None]},
                                        {"$gte": ["$delay_minutes", 15]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "airport": {"$ifNull": ["$_id", "UNKNOWN"]},
                    "flight_count": 1,
                    "with_atd_count": 1,
                    "mean_delay_minutes": 1,
                    "max_delay_minutes": 1,
                    "min_delay_minutes": 1,
                    "delayed_count": 1,
                }
            },
            {"$sort": {"flight_count": -1, "airport": 1}},
            {"$limit": safe_limit},
        ]

        cursor = await self.collection.aggregate(pipeline)
        rows = await cursor.to_list(length=safe_limit)
        if not rows:
            return None
        return rows

    async def get_trajectory_phase_stats(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> list[dict[str, Any]] | None:
        """按垂直状态（I062/200.vert）聚合单航班轨迹的阶段统计。"""
        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {"$project": {"_id": 0, "plots": {"$ifNull": ["$plots", []]}}},
            {"$unwind": "$plots"},
            {
                "$project": {
                    "timestamp": "$plots.time_of_track",
                    "flight_level": "$plots.I062/136.measured_flight_level",
                    "vertical_mode": "$plots.I062/200.vert",
                    "ground_speed_mps": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$isNumber": "$plots.I062/185.vx"},
                                    {"$isNumber": "$plots.I062/185.vy"},
                                ]
                            },
                            {
                                "$sqrt": {
                                    "$add": [
                                        {"$pow": ["$plots.I062/185.vx", 2]},
                                        {"$pow": ["$plots.I062/185.vy", 2]},
                                    ]
                                }
                            },
                            None,
                        ]
                    },
                    "abs_rocd": {
                        "$cond": [
                            {"$isNumber": "$plots.I062/220.rocd"},
                            {"$abs": "$plots.I062/220.rocd"},
                            None,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": "$vertical_mode",
                    "point_count": {"$sum": 1},
                    "start_time": {"$min": "$timestamp"},
                    "end_time": {"$max": "$timestamp"},
                    "min_flight_level": {"$min": "$flight_level"},
                    "max_flight_level": {"$max": "$flight_level"},
                    "average_flight_level": {"$avg": "$flight_level"},
                    "average_ground_speed_mps": {"$avg": "$ground_speed_mps"},
                    "maximum_ground_speed_mps": {"$max": "$ground_speed_mps"},
                    "average_abs_rocd_fpm": {"$avg": "$abs_rocd"},
                    "maximum_abs_rocd_fpm": {"$max": "$abs_rocd"},
                }
            },
            {"$sort": {"start_time": 1}},
        ]

        cursor = await self.collection.aggregate(pipeline)
        rows = await cursor.to_list(length=None)
        if not rows:
            return None
        return rows

    async def get_trajectory_points_for_phases(
        self,
        *,
        batch: str,
        external_id: str | int,
        limit: int = 20000,
    ) -> list[dict[str, Any]]:
        """按时间顺序返回单航班的阶段划分输入点（含垂直状态）。

        M4-11 起阶段秒数按论文口径计算（三维 ENU RDP 分段 + 段内 vert 众数），
        需要逐点的高度与 `I062/200.vert`，不能用按 vert 分组的聚合结果代替；
        上限只用于防御异常超长文档（5 s 采样下不会触顶）。
        """
        safe_limit = self._safe_limit(limit, default=20000, maximum=20000)
        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {"$project": {"_id": 0, "plots": {"$ifNull": ["$plots", []]}}},
            {"$unwind": {"path": "$plots", "includeArrayIndex": "source_index"}},
            {"$sort": {"plots.time_of_track": 1, "source_index": 1}},
            {"$limit": safe_limit},
            {
                "$project": {
                    "_id": 0,
                    "timestamp": "$plots.time_of_track",
                    "lat": "$plots.I062/105.lat",
                    "lon": "$plots.I062/105.lon",
                    "measured_flight_level": "$plots.I062/136.measured_flight_level",
                    "vertical_mode": "$plots.I062/200.vert",
                }
            },
        ]
        cursor = await self.collection.aggregate(pipeline)
        return await cursor.to_list(length=safe_limit)

    async def get_evaluation_inputs(
        self,
        *,
        batch: str,
        external_id: str | int,
        limit: int = 20000,
    ) -> dict[str, list[dict[str, Any]]] | None:
        """M5-05 评估输入：实飞点与全部预测快照。

        - 实飞点已按口径映射为 `timestamp/lat/lon/altitude_m`（米，FL × 30.48，D-17）；
        - 快照保持 `time_stamp/route[]` 结构（route 项含 `eto/lat/lon/afl_value/afl_unit`），
          由 `scat.prediction.alignment.evaluate_flight` 自行解析与插值；
        - 上限只用于防御异常超长文档；找不到航班（或为空）时返回 None。
        """
        safe_limit = self._safe_limit(limit, default=20000, maximum=20000)
        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {
                "$project": {
                    "_id": 0,
                    "plots": {
                        "$map": {
                            "input": {"$slice": [{"$ifNull": ["$plots", []]}, safe_limit]},
                            "as": "plot",
                            "in": {
                                "timestamp": "$$plot.time_of_track",
                                "lat": "$$plot.I062/105.lat",
                                "lon": "$$plot.I062/105.lon",
                                "altitude_m": {
                                    "$cond": [
                                        {"$isNumber": ("$$plot.I062/136.measured_flight_level")},
                                        {
                                            "$multiply": [
                                                ("$$plot.I062/136.measured_flight_level"),
                                                FLIGHT_LEVEL_TO_METRES,
                                            ]
                                        },
                                        None,
                                    ]
                                },
                            },
                        }
                    },
                    "snapshots": {
                        "$map": {
                            "input": {"$ifNull": ["$predicted_trajectory", []]},
                            "as": "snapshot",
                            "in": {
                                "time_stamp": "$$snapshot.time_stamp",
                                "route": "$$snapshot.route",
                            },
                        }
                    },
                }
            },
        ]
        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        if not documents:
            return None
        document = documents[0]
        return {
            "plots": list(document.get("plots") or []),
            "snapshots": list(document.get("snapshots") or []),
        }

    async def get_latest_prediction_snapshot(
        self,
        *,
        batch: str,
        external_id: str | int,
        before: str | None = None,
    ) -> dict[str, Any] | None:
        """返回航班最后一个预测快照（可选：不晚于指定时刻的最后一个）。"""
        snapshot_filter: dict[str, Any]
        if before is not None:
            snapshot_filter = {
                "$filter": {
                    "input": {"$ifNull": ["$predicted_trajectory", []]},
                    "as": "snapshot",
                    "cond": {"$lte": ["$$snapshot.time_stamp", before]},
                }
            }
        else:
            snapshot_filter = {"$ifNull": ["$predicted_trajectory", []]}

        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "source": 1,
                    "identity": 1,
                    "filtered_snapshots": snapshot_filter,
                }
            },
            {"$limit": 1},
        ]

        cursor = await self.collection.aggregate(pipeline)
        documents = await cursor.to_list(length=1)
        if not documents:
            return None
        result = documents[0]
        snapshots = result.get("filtered_snapshots") or []
        if not snapshots:
            return None
        return {
            "id": result.get("id"),
            "source": result.get("source"),
            "identity": result.get("identity"),
            "total_snapshots": None,
            "last_snapshot": snapshots[-1],
        }

    async def get_track_arrays_for_error_stats(
        self,
        *,
        batch: str,
        external_id: str | int,
    ) -> list[dict[str, Any]] | None:
        """返回单航班全部轨迹点（时间、经纬度、飞行高度层），按时间升序。

        仅供预测误差统计使用；普通查询请使用有界分页的 get_track_points。
        """
        pipeline: list[dict[str, Any]] = [
            {"$match": self._source_filter(batch=batch, external_id=external_id)},
            {"$project": {"_id": 0, "plots": {"$ifNull": ["$plots", []]}}},
            {"$unwind": "$plots"},
            {
                "$project": {
                    "timestamp": "$plots.time_of_track",
                    "lat": "$plots.I062/105.lat",
                    "lon": "$plots.I062/105.lon",
                    "flight_level": "$plots.I062/136.measured_flight_level",
                }
            },
            {"$sort": {"timestamp": 1}},
        ]

        cursor = await self.collection.aggregate(pipeline)
        rows = await cursor.to_list(length=None)
        if not rows:
            return None
        return rows
