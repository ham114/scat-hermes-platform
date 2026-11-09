"""M4 物化记录的读取口径：物化文档 → API/服务层字段（只做映射，不访问数据库）。

批次统计与数据质量的字段口径必须与 `scat.schemas.api.BatchStatistics` /
`BatchDataQuality` 一致，物化记录只多版本与保留字段；映射集中在这里，
避免物化读路径与实时聚合路径各写一份。
"""

from __future__ import annotations

from typing import Any

STATISTICS_FIELDS: tuple[str, ...] = (
    "batch",
    "flight_count",
    "unique_callsign_count",
    "aircraft_type_count",
    "departure_airport_count",
    "arrival_airport_count",
    "total_track_points",
    "average_track_points",
    "minimum_track_points",
    "maximum_track_points",
    "flights_with_prediction",
    "total_prediction_snapshots",
)

DATA_QUALITY_FIELDS: tuple[str, ...] = (
    "missing_callsign_count",
    "missing_aircraft_type_count",
    "missing_departure_airport_count",
    "missing_arrival_airport_count",
    "missing_iobt_count",
    "empty_trajectory_count",
    "flights_without_prediction_count",
)


def statistics_payload(document: dict[str, Any]) -> dict[str, Any]:
    """物化批次统计文档 → BatchStatistics 口径字段。"""
    return {field: document.get(field) for field in STATISTICS_FIELDS}


def data_quality_payload(document: dict[str, Any]) -> dict[str, Any]:
    """物化批次统计文档 → BatchDataQuality 口径字段。"""
    return {field: document.get(field) for field in DATA_QUALITY_FIELDS}


__all__ = [
    "DATA_QUALITY_FIELDS",
    "STATISTICS_FIELDS",
    "data_quality_payload",
    "statistics_payload",
]
