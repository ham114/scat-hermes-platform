"""M4 物化聚合口径（只读原始集合，不修改）。"""

from __future__ import annotations

from typing import Any

_PI = 3.141592653589793
_DEG_TO_RAD = _PI / 180.0
_EARTH_RADIUS_M = 6371008.8

# haversine 相邻距离（米）：dlat=lat2-lat1, dlon=lon2-lon1（单位：度）
_HAVERSINE_A = {
    "$add": [
        {
            "$pow": [
                {"$sin": {"$multiply": [{"$subtract": ["$$lat2", "$$lat1"]}, _DEG_TO_RAD / 2]}},
                2,
            ]
        },
        {
            "$multiply": [
                {"$cos": {"$multiply": ["$$lat1", _DEG_TO_RAD]}},
                {"$cos": {"$multiply": ["$$lat2", _DEG_TO_RAD]}},
                {
                    "$pow": [
                        {"$sin": {"$multiply": [{"$subtract": ["$$lon2", "$$lon1"]}, _DEG_TO_RAD / 2]}},
                        2,
                    ]
                },
            ]
        },
    ]
}

_HAVERSINE_DISTANCE_M = {
    "$multiply": [
        _EARTH_RADIUS_M,
        2,
        {"$asin": {"$min": [{"$max": [{"$sqrt": _HAVERSINE_A}, -1]}, 1]}},
    ]
}

# time_of_track 是无时区 ISO-8601 文本，统一转 BSON 日期（不可解析的点为 None 并被过滤）。
_TIME_TO_DATE: dict[str, Any] = {
    "$convert": {"input": "$plots.time_of_track", "to": "date", "onError": None, "onNull": None}
}

# 单航班特征基础聚合：
# - point_count / duration_s / distance（相邻点 haversine 累计，米）
# - aircraft_type；plots 高度/地速极值在任务层从 phase stats 合并
# 位置字段必须与 ScatFlightRepository 轨迹投影一致（经纬度同在 I062/105，不是 I062/100）。
FLIGHT_FEATURE_BASIS_PIPELINE: list[dict[str, Any]] = [
    {
        "$project": {
            "_id": 0,
            "aircraft_type": "$identity.aircraft_type",
            "plots": {"$ifNull": ["$plots", []]},
        }
    },
    {"$unwind": "$plots"},
    # SCAT 的 plots.time_of_track 是无时区 ISO-8601 文本（不是 BSON 日期也不是数字）；
    # 只保留时间可解析、经纬度均为数字的轨迹点（无效点不参与点数/时长/距离）。
    {
        "$match": {
            "$expr": {
                "$and": [
                    {"$ne": [_TIME_TO_DATE, None]},
                    {"$isNumber": "$plots.I062/105.lat"},
                    {"$isNumber": "$plots.I062/105.lon"},
                ]
            }
        }
    },
    {
        "$project": {
            "aircraft_type": 1,
            "t": _TIME_TO_DATE,
            "lat": "$plots.I062/105.lat",
            "lon": "$plots.I062/105.lon",
        }
    },
    {"$sort": {"t": 1}},
    {
        "$group": {
            "_id": None,
            "aircraft_type": {"$first": "$aircraft_type"},
            "point_count": {"$sum": 1},
            "start_t": {"$min": "$t"},
            "end_t": {"$max": "$t"},
            "points": {"$push": {"lat": "$lat", "lon": "$lon"}},
        }
    },
    {
        "$project": {
            "_id": 0,
            "aircraft_type": 1,
            "point_count": 1,
            # BSON 日期相减得毫秒，除以 1000 得到秒（与 FlightFeaturesRecord.duration_s 口径一致）。
            "duration_s": {
                "$divide": [{"$subtract": ["$end_t", "$start_t"]}, 1000]
            },
            # $reduce 的最终值是累加器文档，必须取出 total；否则 distance_m 会是字典。
            "distance_m": {
                "$let": {
                    "vars": {
                        "acc": {
                            "$reduce": {
                                "input": "$points",
                                "initialValue": {"prev": None, "total": 0},
                                "in": {
                                    "$let": {
                                        "vars": {
                                            "lat1": {"$ifNull": ["$$value.prev.lat", 0]},
                                            "lon1": {"$ifNull": ["$$value.prev.lon", 0]},
                                            "lat2": "$$this.lat",
                                            "lon2": "$$this.lon",
                                        },
                                        "in": {
                                            "$cond": [
                                                {"$eq": ["$$value.prev", None]},
                                                {
                                                    "prev": {
                                                        "lat": "$$this.lat",
                                                        "lon": "$$this.lon",
                                                    },
                                                    "total": 0,
                                                },
                                                {
                                                    "prev": {
                                                        "lat": "$$this.lat",
                                                        "lon": "$$this.lon",
                                                    },
                                                    "total": {
                                                        "$add": [
                                                            "$$value.total",
                                                            _HAVERSINE_DISTANCE_M,
                                                        ]
                                                    },
                                                },
                                            ]
                                        },
                                    }
                                },
                            }
                        }
                    },
                    "in": "$$acc.total",
                }
            },
        }
    },
]


async def fetch_flight_feature_basis(
    collection: Any,
    *,
    batch: str,
    external_id: str | int,
) -> dict[str, Any] | None:
    """聚合单航班的点数量、时长、距离（米）与机型。"""
    pipeline = [
        {
            "$match": {
                "source.system": "scat-public-dataset",
                "source.batch": batch,
                "source.external_id": str(external_id),
            }
        },
        *FLIGHT_FEATURE_BASIS_PIPELINE,
    ]
    cursor = await collection.aggregate(pipeline)
    rows = await cursor.to_list(length=1)
    return rows[0] if rows else None


__all__ = ["FLIGHT_FEATURE_BASIS_PIPELINE", "fetch_flight_feature_basis"]
