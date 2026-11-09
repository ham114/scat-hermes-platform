"""UAV 无人机架次数据访问层（flight_data.uav_flights）。

只负责 MongoDB 查询，不负责业务校验、格式化和单位换算。
调用前必须已执行 `await flight_database.connect()`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from core.settings import settings
from scat.infrastructure.database import flight_database

UAV_SOURCE_SYSTEM: Final[str] = "uav-internal-sensor"

# 列表/详情投影：绝不返回 samples 大数组。
FLIGHT_DETAIL_PROJECTION: Final[dict[str, int]] = {
    "_id": 0,
    "source": 1,
    "identity": 1,
    "ingest": 1,
    "summary": 1,
}

# 列表投影更窄：channel_stats（34 项）也不返回。
FLIGHT_SUMMARY_PROJECTION: Final[dict[str, int]] = {
    "_id": 0,
    "source.external_id": 1,
    "source.model": 1,
    "source.tail_number": 1,
    "identity.mission": 1,
    "identity.flight_seq": 1,
    "identity.takeoff_at": 1,
    "identity.duration_s": 1,
    "identity.sample_count": 1,
    "identity.sample_rate_hz": 1,
    "identity.flight_kind": 1,
    "identity.label": 1,
}

# 列表排序字段映射：API 参数 -> 文档字段。
SORT_FIELDS: Final[dict[str, str]] = {
    "takeoff_at": "identity.takeoff_at",
    "external_id": "source.external_id",
    "duration_s": "identity.duration_s",
    "sample_count": "identity.sample_count",
}


class UavFlightRepository:
    """uav_flights 集合的查询封装。"""

    @property
    def collection(self) -> Any:
        return flight_database.get_collection(settings.UAV_FLIGHTS_COLLECTION)

    @staticmethod
    def _system_filter() -> dict[str, Any]:
        return {"source.system": UAV_SOURCE_SYSTEM}

    @staticmethod
    def _safe_limit(limit: int, *, default: int = 20, maximum: int = 100) -> int:
        if limit <= 0:
            return default
        return min(limit, maximum)

    @staticmethod
    def _safe_skip(skip: int) -> int:
        return max(skip, 0)

    @staticmethod
    def build_list_filter(
        *,
        model: str | None = None,
        tail_number: str | None = None,
        mission: str | None = None,
        flight_kind: str | None = None,
        label: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """构造列表查询与计数共用的筛选条件。"""
        filters: dict[str, Any] = UavFlightRepository._system_filter()
        if model:
            filters["source.model"] = model.strip()
        if tail_number:
            filters["source.tail_number"] = tail_number.strip()
        if mission:
            filters["identity.mission"] = mission.strip().upper()
        if flight_kind:
            filters["identity.flight_kind"] = flight_kind.strip()
        if label:
            filters["identity.label"] = label.strip()
        if start_time is not None or end_time is not None:
            takeoff: dict[str, Any] = {}
            if start_time is not None:
                takeoff["$gte"] = start_time
            if end_time is not None:
                takeoff["$lt"] = end_time
            filters["identity.takeoff_at"] = takeoff
        return filters

    async def count_all(self, filters: dict[str, Any] | None = None) -> int:
        return await self.collection.count_documents(filters or self._system_filter())

    async def list_flights(
        self,
        *,
        filters: dict[str, Any] | None = None,
        sort_field: str = "takeoff_at",
        sort_order: int = 1,
        limit: int = 20,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        sort_key = SORT_FIELDS.get(sort_field, SORT_FIELDS["takeoff_at"])
        order = 1 if sort_order >= 0 else -1
        cursor = (
            self.collection.find(filters or self._system_filter(), FLIGHT_SUMMARY_PROJECTION)
            .sort([(sort_key, order), ("source.external_id", 1)])
            .skip(self._safe_skip(skip))
            .limit(self._safe_limit(limit))
        )
        return await cursor.to_list(length=None)

    async def get_by_external_id(
        self,
        external_id: str,
        *,
        include_samples: bool = False,
    ) -> dict[str, Any] | None:
        projection = dict(FLIGHT_DETAIL_PROJECTION)
        if include_samples:
            projection["samples"] = 1
        return await self.collection.find_one(
            {
                "source.system": UAV_SOURCE_SYSTEM,
                "source.external_id": external_id,
            },
            projection,
        )

    async def get_sample_window(
        self,
        external_id: str,
        *,
        start: int,
        limit: int,
    ) -> dict[str, Any] | None:
        """取单个架次 samples 数组的 [start, start+limit) 窗口与总数。"""
        return await self.collection.find_one(
            {
                "source.system": UAV_SOURCE_SYSTEM,
                "source.external_id": external_id,
            },
            {
                "_id": 0,
                "source.external_id": 1,
                "identity.sample_count": 1,
                "identity.takeoff_at": 1,
                "samples": {"$slice": [max(start, 0), limit]},
            },
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """按机型聚合架次数量与起飞时间范围。"""
        cursor = await self.collection.aggregate(
            [
                {"$match": self._system_filter()},
                {
                    "$group": {
                        "_id": "$source.model",
                        "flight_count": {"$sum": 1},
                        "earliest_takeoff": {"$min": "$identity.takeoff_at"},
                        "latest_takeoff": {"$max": "$identity.takeoff_at"},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
        )
        return await cursor.to_list(length=None)

    async def aggregate_channel_stats(
        self,
        *,
        channels: list[str],
        model: str | None = None,
        flight_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """跨架次聚合指定通道的统计（基于每架次 summary.channel_stats）。

        加权均值为 Σ(每架次均值 × 该架次样本数) / Σ样本数；
        不包含标准差（每架次摘要未存平方和，避免一次展开全部 samples）。
        """
        filters: dict[str, Any] = self._system_filter()
        if model:
            filters["source.model"] = model.strip()
        if flight_kind:
            filters["identity.flight_kind"] = flight_kind.strip()

        cursor = await self.collection.aggregate(
            [
                {"$match": filters},
                {
                    "$project": {
                        "_id": 0,
                        "sample_count": {"$ifNull": ["$identity.sample_count", 0]},
                        "channel_stats": {
                            "$ifNull": ["$summary.channel_stats", []],
                        },
                    }
                },
                {"$unwind": "$channel_stats"},
                {"$match": {"channel_stats.key": {"$in": channels}}},
                {
                    "$group": {
                        "_id": "$channel_stats.key",
                        "flight_count": {"$sum": 1},
                        "total_samples": {"$sum": "$sample_count"},
                        "weighted_sum": {
                            "$sum": {
                                "$multiply": [
                                    {"$ifNull": ["$channel_stats.avg", 0]},
                                    "$sample_count",
                                ]
                            }
                        },
                        "min": {"$min": "$channel_stats.min"},
                        "max": {"$max": "$channel_stats.max"},
                        "missing_total": {
                            "$sum": {"$ifNull": ["$channel_stats.missing", 0]},
                        },
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "channel": "$_id",
                        "flight_count": 1,
                        "total_samples": 1,
                        "weighted_mean": {
                            "$cond": [
                                {"$gt": ["$total_samples", 0]},
                                {"$divide": ["$weighted_sum", "$total_samples"]},
                                None,
                            ]
                        },
                        "min": 1,
                        "max": 1,
                        "missing_total": 1,
                    }
                },
                {"$sort": {"channel": 1}},
            ]
        )
        return await cursor.to_list(length=None)

    async def aggregate_kind_channel_stats(
        self,
        *,
        channels: list[str],
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """按样本类型（normal/anomaly_sample）分组的跨架次通道统计。"""
        filters: dict[str, Any] = self._system_filter()
        if model:
            filters["source.model"] = model.strip()

        cursor = await self.collection.aggregate(
            [
                {"$match": filters},
                {
                    "$project": {
                        "_id": 0,
                        "flight_kind": "$identity.flight_kind",
                        "sample_count": {"$ifNull": ["$identity.sample_count", 0]},
                        "channel_stats": {
                            "$ifNull": ["$summary.channel_stats", []],
                        },
                    }
                },
                {"$unwind": "$channel_stats"},
                {"$match": {"channel_stats.key": {"$in": channels}}},
                {
                    "$group": {
                        "_id": {
                            "channel": "$channel_stats.key",
                            "flight_kind": "$flight_kind",
                        },
                        "flight_count": {"$sum": 1},
                        "total_samples": {"$sum": "$sample_count"},
                        "weighted_sum": {
                            "$sum": {
                                "$multiply": [
                                    {"$ifNull": ["$channel_stats.avg", 0]},
                                    "$sample_count",
                                ]
                            }
                        },
                        "min": {"$min": "$channel_stats.min"},
                        "max": {"$max": "$channel_stats.max"},
                        "missing_total": {
                            "$sum": {"$ifNull": ["$channel_stats.missing", 0]},
                        },
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "channel": "$_id.channel",
                        "flight_kind": "$_id.flight_kind",
                        "flight_count": 1,
                        "total_samples": 1,
                        "weighted_mean": {
                            "$cond": [
                                {"$gt": ["$total_samples", 0]},
                                {"$divide": ["$weighted_sum", "$total_samples"]},
                                None,
                            ]
                        },
                        "min": 1,
                        "max": 1,
                        "missing_total": 1,
                    }
                },
                {"$sort": {"channel": 1, "flight_kind": 1}},
            ]
        )
        return await cursor.to_list(length=None)

    async def get_flight_phase_stats(
        self,
        *,
        external_id: str,
    ) -> list[dict[str, Any]] | None:
        """按组合天向速度划分单架次的爬升/平飞/下降阶段并聚合统计。

        阈值固定 ±1.0 m/s：> +1.0 爬升，< -1.0 下降，其余平飞。
        """
        cursor = await self.collection.aggregate(
            [
                {
                    "$match": {
                        "source.system": UAV_SOURCE_SYSTEM,
                        "source.external_id": external_id.strip(),
                    }
                },
                {
                    "$project": {"_id": 0, "samples": {"$ifNull": ["$samples", []]}},
                },
                {"$unwind": "$samples"},
                {
                    "$project": {
                        "phase": {
                            "$switch": {
                                "branches": [
                                    {
                                        "case": {"$gt": ["$samples.cmp_comb_vu", 1.0]},
                                        "then": "climb",
                                    },
                                    {
                                        "case": {"$lt": ["$samples.cmp_comb_vu", -1.0]},
                                        "then": "descent",
                                    },
                                ],
                                "default": "level",
                            }
                        },
                        "t_s": "$samples.t_s",
                        "altitude_m": "$samples.cmp_comb_alt",
                        "ground_speed_mps": "$samples.cmp_ground_speed",
                        "vertical_speed_mps": "$samples.cmp_comb_vu",
                    }
                },
                {
                    "$group": {
                        "_id": "$phase",
                        "point_count": {"$sum": 1},
                        "start_t_s": {"$min": "$t_s"},
                        "end_t_s": {"$max": "$t_s"},
                        "min_altitude_m": {"$min": "$altitude_m"},
                        "max_altitude_m": {"$max": "$altitude_m"},
                        "average_altitude_m": {"$avg": "$altitude_m"},
                        "average_ground_speed_mps": {"$avg": "$ground_speed_mps"},
                        "maximum_ground_speed_mps": {"$max": "$ground_speed_mps"},
                        "average_vertical_speed_mps": {"$avg": "$vertical_speed_mps"},
                    }
                },
                {"$sort": {"start_t_s": 1}},
            ]
        )
        rows = await cursor.to_list(length=None)
        if not rows:
            return None
        return rows
