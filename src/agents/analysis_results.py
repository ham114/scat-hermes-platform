"""Deterministic AnalysisResult builders for aviation tools."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode

from scat.prediction.alignment import haversine_metres
from scat_model_runtime.regression_evaluators import RegressionEvaluation
from scat_model_runtime.task_io import (
    ComparisonSampleResult,
    ComparisonTaskOutput,
    ComparisonTrajectoryPoint,
    RegressionTaskOutput,
)
from schema.analysis import (
    ActionLinkBlock,
    AnalysisResult,
    ClassificationBlock,
    ClassificationItem,
    MetricItem,
    MetricsBlock,
    PredictionBlock,
    PredictionPoint,
    TableBlock,
    TableColumn,
    TimeSeriesBlock,
    TimeSeriesSeries,
    Trajectory2dBlock,
    Trajectory2dSeries,
)
from uav.schemas.channels import CHANNEL_BY_KEY


def _metric(
    statistics: dict[str, Any],
    key: str,
    label: str,
    unit: str | None = None,
) -> MetricItem | None:
    value = statistics.get(key)
    if not isinstance(value, str | int | float | bool) and value is not None:
        return None
    if value is None:
        return None
    return MetricItem(key=key, label=label, value=value, unit=unit)


def _rows(items: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        rows.append({key: item.get(key) for key in keys})
    return rows


def _context(result: dict[str, Any]) -> dict[str, Any]:
    query = result.get("query")
    if not isinstance(query, dict):
        return {}
    return {
        str(key): value
        for key, value in query.items()
        if value is None or isinstance(value, str | int | float | bool)
    }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pagination_metrics(
    pagination: dict[str, Any],
    *,
    returned_key: str,
    item_unit: str,
) -> MetricsBlock:
    return MetricsBlock(
        title="检索结果",
        items=[
            MetricItem(
                key="total",
                label="匹配总数",
                value=int(pagination.get("total") or 0),
                unit=item_unit,
            ),
            MetricItem(
                key="returned",
                label="本页返回",
                value=int(pagination.get(returned_key) or 0),
                unit=item_unit,
            ),
            MetricItem(
                key="has_more",
                label="是否还有下一页",
                value=bool(pagination.get("has_more")),
            ),
        ],
    )


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time_series_blocks(
    rows: list[dict[str, Any]],
    *,
    x_key: str,
    x_label: str,
    x_type: Literal["number", "datetime"],
    x_unit: str | None,
    title_prefix: str,
    series_specs: list[tuple[str, str, str | None]],
) -> list[TimeSeriesBlock]:
    bounded_rows = rows[:200]
    active_specs = [
        spec
        for spec in series_specs
        if any(_numeric(row.get(spec[0])) is not None for row in bounded_rows)
    ]
    grouped: dict[str | None, list[tuple[str, str, str | None]]] = {}
    for spec in active_specs:
        grouped.setdefault(spec[2], []).append(spec)

    blocks: list[TimeSeriesBlock] = []
    for unit, specs in grouped.items():
        for start in range(0, len(specs), 8):
            chunk = specs[start : start + 8]
            points = [
                {
                    x_key: row[x_key],
                    **{key: _numeric(row.get(key)) for key, _, _ in chunk},
                }
                for row in bounded_rows
                if row.get(x_key) is not None
            ]
            blocks.append(
                TimeSeriesBlock(
                    title=f"{title_prefix}（{unit or '无单位'}）",
                    x_key=x_key,
                    x_label=x_label,
                    x_type=x_type,
                    x_unit=x_unit,
                    series=[
                        TimeSeriesSeries(key=key, label=label, unit=series_unit)
                        for key, label, series_unit in chunk
                    ],
                    points=points,
                )
            )
    return blocks


def _trajectory_2d_block(
    rows: list[dict[str, Any]],
    *,
    x_key: str,
    y_key: str,
    x_label: str,
    y_label: str,
    x_unit: str | None,
    y_unit: str | None,
    time_key: str | None,
    title: str,
    series_specs: list[tuple[str, str, str | None]] | None = None,
    path_key: str | None = None,
    path_label: str | None = None,
    segment_key: str | None = None,
    segment_label: str | None = None,
    snapshot_key: str | None = None,
    snapshot_label: str | None = None,
) -> Trajectory2dBlock | None:
    """从行数据构建二维轨迹块；有效坐标点不足 2 个时返回 None。"""
    specs = series_specs or []
    series = [
        Trajectory2dSeries(key=key, label=label, unit=unit, role="color")
        for key, label, unit in specs
    ]
    points: list[dict[str, Any]] = []
    for row in rows[:5_000]:
        x = _numeric(row.get(x_key))
        y = _numeric(row.get(y_key))
        if x is None or y is None:
            continue
        point: dict[str, Any] = {x_key: x, y_key: y}
        if time_key:
            time_value = _numeric(row.get(time_key))
            if time_value is not None:
                point[time_key] = time_value
        for key, _label, _unit in specs:
            value = _numeric(row.get(key))
            if value is not None:
                point[key] = value
        for group_key in (path_key, segment_key, snapshot_key):
            if group_key and isinstance(row.get(group_key), str) and row[group_key]:
                point[group_key] = row[group_key]
        points.append(point)
    if len(points) < 2:
        return None
    return Trajectory2dBlock(
        title=title,
        x_key=x_key,
        x_label=x_label,
        x_unit=x_unit,
        y_key=y_key,
        y_label=y_label,
        y_unit=y_unit,
        time_key=time_key,
        path_key=path_key,
        path_label=path_label,
        segment_key=segment_key,
        segment_label=segment_label,
        snapshot_key=snapshot_key,
        snapshot_label=snapshot_label,
        series=series,
        points=points,
    )


def build_scat_batch_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build metric and distribution blocks for a SCAT batch overview."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    statistics = data.get("statistics") if isinstance(data.get("statistics"), dict) else {}
    batch = str(statistics.get("batch") or result.get("query", {}).get("batch") or "SCAT")
    metric_specs = (
        ("flight_count", "航班数", "条"),
        ("unique_callsign_count", "唯一呼号", "个"),
        ("total_track_points", "轨迹点", "个"),
        ("flights_with_prediction", "含空管预测航班", "条"),
    )
    metrics = [
        item
        for key, label, unit in metric_specs
        if (item := _metric(statistics, key, label, unit)) is not None
    ]
    blocks: list[MetricsBlock | TableBlock] = []
    if metrics:
        blocks.append(MetricsBlock(title="批次核心指标", items=metrics))

    aircraft_rows = _rows(data.get("top_aircraft_types"), ("aircraft_type", "flight_count"))
    if aircraft_rows:
        blocks.append(
            TableBlock(
                title="主要机型",
                columns=[
                    TableColumn(key="aircraft_type", label="ICAO 机型"),
                    TableColumn(key="flight_count", label="航班数", unit="条"),
                ],
                rows=aircraft_rows,
            )
        )

    route_rows = _rows(data.get("top_routes"), ("adep", "ades", "flight_count"))
    if route_rows:
        blocks.append(
            TableBlock(
                title="主要航线",
                columns=[
                    TableColumn(key="adep", label="起飞机场"),
                    TableColumn(key="ades", label="目的机场"),
                    TableColumn(key="flight_count", label="航班数", unit="条"),
                ],
                rows=route_rows,
            )
        )

    if not blocks:
        return None
    return AnalysisResult(
        dataset="scat",
        operation="get_scat_batch_overview",
        title=f"SCAT 批次 {batch} 统计",
        context={"batch": batch},
        blocks=blocks,
    )


def build_uav_model_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build metric and table blocks for the UAV model catalog."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    items = data.get("items") if isinstance(data.get("items"), list) else []
    rows = _rows(
        items,
        ("model", "flight_count", "earliest_takeoff_utc", "latest_takeoff_utc"),
    )
    total_flights = sum(
        row["flight_count"] for row in rows if isinstance(row.get("flight_count"), int | float)
    )
    return AnalysisResult(
        dataset="uav",
        operation="list_uav_models",
        title="UAV 机型与架次统计",
        blocks=[
            MetricsBlock(
                title="数据规模",
                items=[
                    MetricItem(
                        key="flight_count",
                        label="总架次数",
                        value=total_flights,
                        unit="架次",
                    ),
                    MetricItem(
                        key="model_count",
                        label="机型/分组数",
                        value=len(rows),
                        unit="个",
                    ),
                ],
            ),
            TableBlock(
                title="机型分布",
                columns=[
                    TableColumn(key="model", label="机型/分组"),
                    TableColumn(key="flight_count", label="架次数", unit="架次"),
                    TableColumn(
                        key="earliest_takeoff_utc",
                        label="最早起飞时间（UTC）",
                    ),
                    TableColumn(
                        key="latest_takeoff_utc",
                        label="最晚起飞时间（UTC）",
                    ),
                ],
                rows=rows,
            ),
        ],
    )


def build_scat_flight_search_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build a bounded table for a SCAT flight search result."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    items = data.get("items") if isinstance(data.get("items"), list) else []
    rows: list[dict[str, Any]] = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        source: dict[str, Any] = _mapping(item.get("source"))
        identity: dict[str, Any] = _mapping(item.get("identity"))
        rows.append(
            {
                "external_id": source.get("external_id"),
                "callsign": identity.get("callsign"),
                "aircraft_type": identity.get("aircraft_type"),
                "departure_airport": identity.get("departure_airport"),
                "destination_airport": identity.get("destination_airport"),
                "iobt_utc": identity.get("iobt_utc"),
                "control_centre_transfer_count": item.get("control_centre_transfer_count"),
            }
        )

    return AnalysisResult(
        dataset="scat",
        operation="search_scat_flights",
        title="SCAT 航班检索结果",
        context=_context(result),
        blocks=[
            _pagination_metrics(data, returned_key="count", item_unit="条"),
            TableBlock(
                title="航班列表",
                columns=[
                    TableColumn(key="external_id", label="外部航班 ID"),
                    TableColumn(key="callsign", label="呼号"),
                    TableColumn(key="aircraft_type", label="ICAO 机型"),
                    TableColumn(key="departure_airport", label="起飞机场"),
                    TableColumn(key="destination_airport", label="目的机场"),
                    TableColumn(key="iobt_utc", label="计划起飞时间（UTC）"),
                    TableColumn(
                        key="control_centre_transfer_count",
                        label="管制中心移交次数",
                        unit="次",
                    ),
                ],
                rows=rows,
            ),
        ],
    )


def build_scat_flight_overview_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build deterministic metrics for a single SCAT flight overview."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    flight = _mapping(data.get("flight"))
    source = _mapping(flight.get("source"))
    identity = _mapping(flight.get("identity"))
    trajectory = _mapping(data.get("trajectory"))
    altitude = _mapping(trajectory.get("altitude"))
    speed = _mapping(trajectory.get("ground_speed"))
    quality = _mapping(trajectory.get("data_quality"))
    prediction = _mapping(data.get("scat_prediction"))
    departure = identity.get("departure_airport")
    destination = identity.get("destination_airport")
    route = (
        f"{departure} → {destination}"
        if isinstance(departure, str) and isinstance(destination, str)
        else None
    )
    statistics = {
        "callsign": identity.get("callsign"),
        "aircraft_type": identity.get("aircraft_type"),
        "route": route,
        "iobt_utc": identity.get("iobt_utc"),
        "point_count": trajectory.get("point_count"),
        "duration_minutes": trajectory.get("duration_minutes"),
        "average_altitude_m": altitude.get("average_metres"),
        "maximum_altitude_m": altitude.get("maximum_metres"),
        "average_speed_mps": speed.get("average_mps"),
        "maximum_speed_mps": speed.get("maximum_mps"),
        "is_model_input_candidate": quality.get("is_model_input_candidate"),
        "prediction_snapshot_count": prediction.get("snapshot_count"),
    }
    metric_specs = (
        ("callsign", "呼号", None),
        ("aircraft_type", "ICAO 机型", None),
        ("route", "航线", None),
        ("iobt_utc", "计划起飞时间（UTC）", None),
        ("point_count", "轨迹点", "个"),
        ("duration_minutes", "轨迹时长", "min"),
        ("average_altitude_m", "平均高度", "m"),
        ("maximum_altitude_m", "最高高度", "m"),
        ("average_speed_mps", "平均地速", "m/s"),
        ("maximum_speed_mps", "最大地速", "m/s"),
        ("is_model_input_candidate", "可作为模型输入", None),
        ("prediction_snapshot_count", "SCAT 预测快照", "个"),
    )
    metrics = [
        item
        for key, label, unit in metric_specs
        if (item := _metric(statistics, key, label, unit)) is not None
    ]

    mode_counts = _mapping(trajectory.get("vertical_mode_counts"))
    mode_ratios = _mapping(trajectory.get("vertical_mode_ratios"))
    labels = {
        "level": "平飞",
        "climb": "爬升",
        "descent": "下降",
        "uncertain": "不确定",
    }
    mode_rows = [
        {
            "mode": key,
            "label": label,
            "count": mode_counts.get(key, 0),
            "ratio": mode_ratios.get(key, 0),
        }
        for key, label in labels.items()
    ]
    external_id = source.get("external_id") or result.get("query", {}).get("external_id")
    title_id = str(external_id or "SCAT")
    blocks: list[MetricsBlock | TableBlock] = []
    if metrics:
        blocks.append(MetricsBlock(title="航班核心指标", items=metrics))
    blocks.append(
        TableBlock(
            title="垂直状态分布",
            columns=[
                TableColumn(key="label", label="状态"),
                TableColumn(key="count", label="轨迹点", unit="个"),
                TableColumn(key="ratio", label="占比"),
            ],
            rows=mode_rows,
        )
    )
    return AnalysisResult(
        dataset="scat",
        operation="get_scat_flight_overview",
        title=f"SCAT 航班 {title_id} 概览",
        context=_context(result),
        blocks=blocks,
    )


def build_uav_flight_search_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build a bounded table for a UAV flight search result."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
    rows = _rows(
        data.get("items"),
        (
            "external_id",
            "model",
            "tail_number",
            "mission",
            "flight_seq",
            "takeoff_at",
            "duration_s",
            "sample_count",
            "sample_rate_hz",
            "flight_kind",
            "label",
        ),
    )
    return AnalysisResult(
        dataset="uav",
        operation="search_uav_flights",
        title="UAV 架次检索结果",
        context=_context(result),
        blocks=[
            _pagination_metrics(
                pagination,
                returned_key="returned",
                item_unit="架次",
            ),
            TableBlock(
                title="架次列表",
                columns=[
                    TableColumn(key="external_id", label="架次 ID"),
                    TableColumn(key="model", label="机型/分组"),
                    TableColumn(key="tail_number", label="机尾号"),
                    TableColumn(key="mission", label="任务"),
                    TableColumn(key="flight_seq", label="架次序号"),
                    TableColumn(key="takeoff_at", label="起飞时间（UTC）"),
                    TableColumn(key="duration_s", label="时长", unit="s"),
                    TableColumn(key="sample_count", label="样本数", unit="个"),
                    TableColumn(key="sample_rate_hz", label="采样率", unit="Hz"),
                    TableColumn(key="flight_kind", label="样本类型"),
                    TableColumn(key="label", label="标签"),
                ],
                rows=rows,
            ),
        ],
    )


def build_uav_flight_overview_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build flight metrics and unit-aware channel statistics for one UAV flight."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    summary: dict[str, Any] = _mapping(data.get("summary"))
    altitude: dict[str, Any] = _mapping(summary.get("altitude_m"))
    speed: dict[str, Any] = _mapping(summary.get("ground_speed_mps"))
    statistics = {
        **data,
        "altitude_min": altitude.get("min"),
        "altitude_max": altitude.get("max"),
        "ground_speed_min": speed.get("min"),
        "ground_speed_max": speed.get("max"),
    }
    metric_specs = (
        ("model", "机型/分组", None),
        ("tail_number", "机尾号", None),
        ("mission", "任务", None),
        ("takeoff_at", "起飞时间（UTC）", None),
        ("duration_s", "时长", "s"),
        ("sample_count", "样本数", "个"),
        ("sample_rate_hz", "采样率", "Hz"),
        ("flight_kind", "样本类型", None),
        ("altitude_min", "最低组合高度", "m"),
        ("altitude_max", "最高组合高度", "m"),
        ("ground_speed_min", "最低地速", "m/s"),
        ("ground_speed_max", "最高地速", "m/s"),
    )
    metrics = [
        item
        for key, label, unit in metric_specs
        if (item := _metric(statistics, key, label, unit)) is not None
    ]

    channel_rows: list[dict[str, Any]] = []
    channel_stats = data.get("channel_stats")
    if isinstance(channel_stats, list):
        for item in channel_stats[:100]:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            spec = CHANNEL_BY_KEY.get(key) if isinstance(key, str) else None
            channel_rows.append(
                {
                    "key": key,
                    "label": spec.label if spec else key,
                    "group": spec.group if spec else None,
                    "unit": spec.unit if spec else None,
                    "min": item.get("min"),
                    "max": item.get("max"),
                    "avg": item.get("avg"),
                    "missing": item.get("missing"),
                }
            )

    external_id = str(data.get("external_id") or "UAV")
    blocks: list[MetricsBlock | TableBlock] = []
    if metrics:
        blocks.append(MetricsBlock(title="架次核心指标", items=metrics))
    blocks.append(
        TableBlock(
            title="传感器通道统计",
            columns=[
                TableColumn(key="key", label="通道键"),
                TableColumn(key="label", label="通道名称"),
                TableColumn(key="group", label="通道组"),
                TableColumn(key="unit", label="单位"),
                TableColumn(key="min", label="最小值"),
                TableColumn(key="max", label="最大值"),
                TableColumn(key="avg", label="平均值"),
                TableColumn(key="missing", label="缺失数", unit="个"),
            ],
            rows=channel_rows,
        )
    )
    return AnalysisResult(
        dataset="uav",
        operation="get_uav_flight_overview",
        title=f"UAV 架次 {external_id} 统计",
        context={"external_id": external_id},
        blocks=blocks,
    )


def build_scat_track_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build unit-separated time-series blocks from normalized SCAT track points."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    pagination = _mapping(data.get("pagination"))
    source_points = data.get("points") if isinstance(data.get("points"), list) else []
    rows: list[dict[str, Any]] = []
    for point in source_points[:200]:
        if not isinstance(point, dict):
            continue
        position = _mapping(point.get("position"))
        motion = _mapping(point.get("motion"))
        mode_s = _mapping(point.get("mode_s"))
        rows.append(
            {
                "sequence": point.get("sequence"),
                "timestamp": point.get("timestamp"),
                "latitude": position.get("lat_deg"),
                "longitude": position.get("lon_deg"),
                "altitude": position.get("altitude_m"),
                "ground_speed": motion.get("ground_speed_mps"),
                "vertical_speed": motion.get("vertical_speed_mps"),
                "track_angle": motion.get("track_angle_deg"),
                "indicated_airspeed": mode_s.get("indicated_airspeed_knots"),
                "mach": mode_s.get("mach"),
            }
        )

    parsed_times = [_utc_datetime(row.get("timestamp")) for row in rows]
    has_time_axis = bool(rows) and all(value is not None for value in parsed_times)
    start_time: str | None = None
    if has_time_axis:
        origin = parsed_times[0]
        assert origin is not None
        start_time = _utc_text(origin)
        for row, timestamp in zip(rows, parsed_times, strict=True):
            assert timestamp is not None
            row["time_step_s"] = round((timestamp - origin).total_seconds(), 6)
    blocks: list[MetricsBlock | TimeSeriesBlock | Trajectory2dBlock] = [
        MetricsBlock(
            title="轨迹窗口",
            items=[
                MetricItem(
                    key="total_points",
                    label="轨迹总点数",
                    value=int(pagination.get("total_points") or 0),
                    unit="个",
                ),
                MetricItem(
                    key="returned_points",
                    label="本页展示点",
                    value=len(rows),
                    unit="个",
                ),
                MetricItem(
                    key="every_nth",
                    label="降采样步长",
                    value=int(result.get("query", {}).get("every_nth") or 1),
                ),
                MetricItem(
                    key="has_more",
                    label="是否还有下一页",
                    value=bool(pagination.get("has_more")),
                ),
            ],
        )
    ]
    if has_time_axis:
        blocks.extend(
            _time_series_blocks(
                rows,
                x_key="time_step_s",
                x_label="时间步",
                x_type="number",
                x_unit="s",
                title_prefix="SCAT 轨迹时序",
                series_specs=[
                    ("latitude", "纬度", "deg"),
                    ("longitude", "经度", "deg"),
                    ("altitude", "高度", "m"),
                    ("ground_speed", "地速", "m/s"),
                    ("vertical_speed", "垂直速度", "m/s"),
                    ("track_angle", "航迹角", "deg"),
                    ("indicated_airspeed", "指示空速", "kt"),
                    ("mach", "马赫数", None),
                ],
            )
        )
    trajectory = _trajectory_2d_block(
        rows,
        x_key="longitude",
        y_key="latitude",
        x_label="经度",
        y_label="纬度",
        x_unit="deg",
        y_unit="deg",
        time_key="time_step_s" if has_time_axis else None,
        title="SCAT 二维轨迹",
        series_specs=[("altitude", "高度", "m")],
    )
    if trajectory is not None:
        blocks.append(trajectory)
    flight = _mapping(data.get("flight"))
    source = _mapping(flight.get("source"))
    external_id = source.get("external_id") or result.get("query", {}).get("external_id")
    return AnalysisResult(
        schema_version=("1.3" if trajectory is not None else ("1.2" if has_time_axis else "1.0")),
        dataset="scat",
        operation="get_scat_track_points",
        title=f"SCAT 航班 {external_id or 'SCAT'} 轨迹分析",
        start_time=start_time,
        context=_context(result),
        blocks=blocks,
    )


def build_uav_samples_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build unit-separated time-series blocks from bounded UAV sensor samples."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    samples = data.get("samples") if isinstance(data.get("samples"), list) else []
    rows = [dict(item) for item in samples[:200] if isinstance(item, dict)]
    start_time_value = data.get("start_time")
    parsed_start_time = _utc_datetime(start_time_value)
    origin_t_s = _numeric(rows[0].get("t_s")) if rows else None
    has_time_axis = parsed_start_time is not None and origin_t_s is not None
    if has_time_axis:
        assert origin_t_s is not None
        for row in rows:
            t_s = _numeric(row.get("t_s"))
            row["time_step_s"] = round(t_s - origin_t_s, 6) if t_s is not None else None
    active_keys = {
        key
        for row in rows
        for key, value in row.items()
        if key in CHANNEL_BY_KEY and _numeric(value) is not None
    }
    series_specs: list[tuple[str, str, str | None]] = [
        (channel.key, channel.label, channel.unit)
        for channel in CHANNEL_BY_KEY.values()
        if channel.key in active_keys
    ]
    blocks: list[MetricsBlock | TimeSeriesBlock | Trajectory2dBlock] = [
        MetricsBlock(
            title="采样窗口",
            items=[
                MetricItem(
                    key="source_window",
                    label="源窗口大小",
                    value=int(data.get("limit") or 0),
                    unit="个",
                ),
                MetricItem(
                    key="returned",
                    label="本页展示点",
                    value=len(rows),
                    unit="个",
                ),
                MetricItem(
                    key="every_nth",
                    label="降采样步长",
                    value=int(data.get("every_nth") or 1),
                ),
                MetricItem(
                    key="has_more",
                    label="是否还有下一页",
                    value=bool(data.get("has_more")),
                ),
            ],
        )
    ]
    if has_time_axis:
        blocks.extend(
            _time_series_blocks(
                rows,
                x_key="time_step_s",
                x_label="时间步",
                x_type="number",
                x_unit="s",
                title_prefix="UAV 传感器时序",
                series_specs=series_specs,
            )
        )
    trajectory = None
    if "cmp_comb_lon" in active_keys and "cmp_comb_lat" in active_keys:
        trajectory = _trajectory_2d_block(
            rows,
            x_key="cmp_comb_lon",
            y_key="cmp_comb_lat",
            x_label="经度",
            y_label="纬度",
            x_unit="deg",
            y_unit="deg",
            time_key="time_step_s" if has_time_axis else None,
            title="UAV 二维轨迹",
            series_specs=(
                [("cmp_comb_alt", "组合高度", "m")] if "cmp_comb_alt" in active_keys else None
            ),
        )
    if trajectory is not None:
        blocks.append(trajectory)
    external_id = str(data.get("external_id") or "UAV")
    return AnalysisResult(
        schema_version=("1.3" if trajectory is not None else ("1.2" if has_time_axis else "1.0")),
        dataset="uav",
        operation="get_uav_samples",
        title=f"UAV 架次 {external_id} 传感器分析",
        start_time=_utc_text(parsed_start_time) if parsed_start_time else None,
        context=_context(result),
        blocks=blocks,
    )


def attach_analysis_result(
    result: dict[str, Any],
    analysis: AnalysisResult | None,
) -> dict[str, Any]:
    """Return a new tool payload containing a JSON-safe structured result."""
    if analysis is None:
        return result
    return {
        **result,
        "analysis_result": analysis.model_dump(mode="json"),
    }


def build_regression_model_analysis(
    output: RegressionTaskOutput,
    evaluation: RegressionEvaluation | None = None,
    *,
    artifact_id: str | None = None,
    thread_id: str | None = None,
    visualization_base_url: str = "/",
) -> AnalysisResult:
    """Render one or many regression series with explicit trajectory segments."""
    prediction = output.series[0]
    source = f"{output.model_key}:{output.model_version}"
    label_by_key = {
        "lat": "纬度",
        "lon": "经度",
        "measured_flight_level": "高度",
        "vx": "东西向速度",
        "vy": "南北向速度",
        "rocd": "垂直速度",
    }
    blocks: list[Any] = [
        MetricsBlock(
            title="模型预测摘要",
            items=[
                MetricItem(key="model", label="模型", value=source),
                MetricItem(
                    key="batch_size",
                    label="轨迹数",
                    value=output.batch_size,
                    unit="条",
                ),
                MetricItem(
                    key="forecast_points",
                    label="预测点总数",
                    value=sum(len(item.values) for item in output.series),
                    unit="个",
                ),
                MetricItem(
                    key="forecast_horizon_seconds",
                    label="预测时域",
                    value=prediction.time_offsets_seconds[-1],
                    unit="s",
                ),
            ],
        )
    ]
    if evaluation is not None:
        evaluation_items = [
            MetricItem(
                key="evaluated_series_count",
                label="已评估轨迹",
                value=evaluation.evaluated_series_count,
                unit="条",
            ),
            MetricItem(
                key="skipped_series_count",
                label="跳过轨迹",
                value=evaluation.skipped_series_count,
                unit="条",
            ),
        ]
        if evaluation.ade_metres is not None:
            evaluation_items.append(
                MetricItem(
                    key="ade_metres",
                    label="水平 ADE",
                    value=evaluation.ade_metres,
                    unit="m",
                )
            )
        if evaluation.fde_metres is not None:
            evaluation_items.append(
                MetricItem(
                    key="fde_metres",
                    label="水平 FDE",
                    value=evaluation.fde_metres,
                    unit="m",
                )
            )
        blocks.extend(
            [
                MetricsBlock(title="回归误差摘要", items=evaluation_items),
                TableBlock(
                    title="各特征误差",
                    columns=[
                        TableColumn(key="target_name", label="特征"),
                        TableColumn(key="sample_count", label="有效配对点"),
                        TableColumn(key="rmse", label="RMSE"),
                        TableColumn(key="mae", label="MAE"),
                        TableColumn(key="unit", label="单位"),
                    ],
                    rows=[item.model_dump(mode="json") for item in evaluation.target_errors],
                ),
            ]
        )
        if evaluation.trajectory_errors:
            blocks.append(
                TableBlock(
                    title="逐轨迹位置误差",
                    columns=[
                        TableColumn(key="external_id", label="轨迹编号"),
                        TableColumn(key="batch", label="批次"),
                        TableColumn(key="sample_count", label="有效位置点"),
                        TableColumn(key="ade_metres", label="ADE", unit="m"),
                        TableColumn(key="fde_metres", label="FDE", unit="m"),
                    ],
                    rows=[item.model_dump(mode="json") for item in evaluation.trajectory_errors],
                )
            )
    if output.batch_size == 1:
        for column, target in enumerate(output.target_names[:8]):
            blocks.append(
                PredictionBlock(
                    title=f"{label_by_key.get(target, target)}预测",
                    source=source,
                    target_label=label_by_key.get(target, target),
                    target_unit=output.target_units[target],
                    points=[
                        PredictionPoint(time_step_s=offset, value=row[column])
                        for offset, row in zip(
                            prediction.time_offsets_seconds[:200],
                            prediction.values[:200],
                            strict=True,
                        )
                    ],
                )
            )
    else:
        blocks.append(
            TableBlock(
                title="批量预测明细",
                columns=[
                    TableColumn(key="external_id", label="轨迹编号"),
                    TableColumn(key="forecast_origin", label="预测起点"),
                    TableColumn(key="forecast_points", label="预测点数"),
                    TableColumn(key="forecast_horizon", label="预测时域", unit="s"),
                ],
                rows=[
                    {
                        "external_id": item.input_ref.external_id,
                        "forecast_origin": item.forecast_origin.isoformat(),
                        "forecast_points": len(item.values),
                        "forecast_horizon": item.time_offsets_seconds[-1],
                    }
                    for item in output.series
                ],
            )
        )

    if "lat" in output.target_names and "lon" in output.target_names:
        lat_column = output.target_names.index("lat")
        lon_column = output.target_names.index("lon")
        altitude_column = (
            output.target_names.index("measured_flight_level")
            if "measured_flight_level" in output.target_names
            else None
        )
        rows: list[dict[str, Any]] = []
        for item in output.series:
            trajectory_id = item.input_ref.external_id
            observed = item.annotations.get("observed_trajectory")
            if isinstance(observed, list):
                for raw in observed:
                    if isinstance(raw, dict):
                        rows.append({**raw, "trajectory_id": trajectory_id, "segment": "observed"})

            # Repeat the observed origin as the first predicted point so the two
            # differently colored paths meet without joining unrelated flights.
            if isinstance(observed, list) and observed and isinstance(observed[-1], dict):
                rows.append(
                    {
                        **observed[-1],
                        "time_step_s": 0.0,
                        "trajectory_id": trajectory_id,
                        "segment": "predicted",
                    }
                )
            for offset, values in zip(
                item.time_offsets_seconds[:500],
                item.values[:500],
                strict=True,
            ):
                point = {
                    "lat": values[lat_column],
                    "lon": values[lon_column],
                    "time_step_s": offset,
                    "trajectory_id": trajectory_id,
                    "segment": "predicted",
                }
                if altitude_column is not None:
                    point["altitude"] = values[altitude_column]
                rows.append(point)

            if output.batch_size == 1:
                actual = item.annotations.get("actual_future_trajectory")
                if isinstance(actual, list) and actual:
                    if isinstance(observed, list) and observed and isinstance(observed[-1], dict):
                        rows.append(
                            {
                                **observed[-1],
                                "time_step_s": 0.0,
                                "trajectory_id": trajectory_id,
                                "segment": "actual_future",
                            }
                        )
                    for raw in actual:
                        if isinstance(raw, dict):
                            rows.append(
                                {
                                    **raw,
                                    "trajectory_id": trajectory_id,
                                    "segment": "actual_future",
                                }
                            )
        trajectory = _trajectory_2d_block(
            rows,
            x_key="lon",
            y_key="lat",
            x_label="经度",
            y_label="纬度",
            x_unit="degree",
            y_unit="degree",
            time_key="time_step_s",
            title="观测与预测二维轨迹",
            series_specs=(
                [("altitude", "高度", output.target_units["measured_flight_level"])]
                if altitude_column is not None
                else None
            ),
            path_key="trajectory_id",
            path_label="轨迹编号",
            segment_key="segment",
            segment_label="轨迹分段",
        )
        if trajectory is not None:
            blocks.append(trajectory)

    input_ref = prediction.input_ref
    context = {
        "external_id": input_ref.external_id,
        "external_ids": ",".join(item.input_ref.external_id for item in output.series),
        "model_key": output.model_key,
        "model_version": output.model_version,
    }
    if input_ref.batch is not None:
        context["batch"] = input_ref.batch
    classification_items = []
    probability_rows = []
    class_count = None
    for series in output.series:
        probabilities = series.annotations.get("class_probabilities")
        if not isinstance(probabilities, list):
            continue
        for vector in probabilities:
            if (
                not isinstance(vector, (list, tuple))
                or not 2 <= len(vector) <= 20
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or not 0 <= v <= 1
                    for v in vector
                )
                or not math.isclose(sum(vector), 1, abs_tol=1e-5)
                or (class_count is not None and class_count != len(vector))
            ):
                continue
            class_count = len(vector)
            winner = max(range(len(vector)), key=vector.__getitem__)
            classification_items.append(
                ClassificationItem(
                    label=f"类别索引 {winner}",
                    confidence=vector[winner],
                    sample_ref=series.input_ref.external_id,
                )
            )
            probability_rows.extend(
                {"sample": series.input_ref.external_id, "class_index": i, "probability": value}
                for i, value in enumerate(vector)
            )
    if classification_items:
        assert class_count is not None
        context["classification_semantics"] = (
            "模型辅助头，类别语义未登记；不解释为正常/异常或飞行阶段，不生成准确率"
        )
        context["classification_probability_total_rows"] = len(probability_rows)
        context["classification_probability_displayed_rows"] = min(100, len(probability_rows))
        blocks.append(
            MetricsBlock(
                title="辅助分类头解释边界",
                items=[
                    MetricItem(
                        key="classification_semantics",
                        label="类别语义",
                        value="未登记，仅展示类别索引；不判断正常/异常或阶段",
                    ),
                    MetricItem(
                        key="classification_quality",
                        label="分类科学质量",
                        value="尚未准入，不生成准确率",
                    ),
                ],
            )
        )
        blocks.append(
            ClassificationBlock(
                title="GBAR辅助分类头（仅类别索引）",
                class_labels=[f"类别索引 {i}" for i in range(class_count)],
                items=classification_items[:200],
            )
        )
        blocks.append(
            TableBlock(
                title="辅助分类头原始概率",
                columns=[
                    TableColumn(key="sample", label="样本"),
                    TableColumn(key="class_index", label="类别索引"),
                    TableColumn(key="probability", label="模型概率"),
                ],
                rows=probability_rows[:100],
            )
        )
    if artifact_id is not None:
        base_url = visualization_base_url.rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("visualization_base_url must be http(s) or the same-origin root")
        url = f"{base_url}/visualizations/{quote(artifact_id, safe='')}/3d"
        if thread_id:
            url = f"{url}?{urlencode({'thread_id': thread_id})}"
        blocks.append(
            ActionLinkBlock(
                label="打开三维轨迹视图",
                url=url,
                kind="visualization_3d",
                artifact_id=artifact_id,
                thread_id=thread_id,
            )
        )
    return AnalysisResult(
        schema_version="1.4",
        dataset=input_ref.dataset.value,
        operation="run_regression_model",
        title=f"{source} 回归预测结果",
        start_time=_utc_text(prediction.forecast_origin),
        context=context,
        blocks=blocks,
    )


def build_comparison_model_analysis(
    output: ComparisonTaskOutput,
    *,
    artifact_id: str | None = None,
    thread_id: str | None = None,
    visualization_base_url: str = "/",
) -> AnalysisResult:
    """Render a traceable common-condition method comparison as bounded blocks."""
    samples = output.samples
    origins = [sample.forecast_origin for sample in samples]
    flights = {sample.input_ref.external_id for sample in samples}
    excluded_count = sum(output.excluded_counts.values())
    attempted_count = len(samples) + excluded_count
    coverage = len(samples) / attempted_count if attempted_count else 0.0
    method_rows = [
        {
            "method": summary.display_name,
            "method_id": summary.method_id,
            "ade_median_m": summary.ade_metres.median,
            "ade_mean_m": summary.ade_metres.mean,
            "fde_median_m": summary.fde_metres.median,
            "sample_count": summary.sample_count,
            "covered_flights": summary.covered_flights,
            "coverage": coverage,
            "version": summary.method_version,
        }
        for summary in output.method_summaries
    ]
    blocks: list[Any] = [
        TableBlock(
            title="统一口径预测方法对比",
            columns=[
                TableColumn(key="method", label="方法"),
                TableColumn(key="method_id", label="方法 ID"),
                TableColumn(key="ade_median_m", label="ADE 中位数", unit="m"),
                TableColumn(key="ade_mean_m", label="ADE 均值", unit="m"),
                TableColumn(key="fde_median_m", label="FDE 中位数", unit="m"),
                TableColumn(key="sample_count", label="样本量", unit="个"),
                TableColumn(key="covered_flights", label="覆盖航班", unit="架"),
                TableColumn(key="coverage", label="有效样本覆盖率"),
                TableColumn(key="version", label="方法版本"),
            ],
            rows=method_rows,
        ),
        MetricsBlock(
            title="统一评估口径",
            items=[
                MetricItem(
                    key="protocol_version",
                    label="协议版本",
                    value=output.protocol_version,
                ),
                MetricItem(
                    key="observation_scope",
                    label="观测条件",
                    value="同一航班、同一 cutoff、同一 GBAR 处理后观测窗",
                ),
                MetricItem(
                    key="forecast_axis",
                    label="预测条件",
                    value="同一 forecast origin 与同一预测时刻轴",
                ),
                MetricItem(
                    key="truth_alignment",
                    label="真值对齐",
                    value="最近候选匹配，时间容差 6.0 s",
                ),
                MetricItem(
                    key="sample_set_sha256",
                    label="样本集合 SHA-256",
                    value=output.sample_set_sha256,
                ),
            ],
        ),
        MetricsBlock(
            title="试点规模与时间",
            items=[
                MetricItem(key="sample_count", label="有效样本", value=len(samples), unit="个"),
                MetricItem(key="flight_count", label="覆盖航班", value=len(flights), unit="架"),
                MetricItem(
                    key="excluded_count",
                    label="排除样本",
                    value=excluded_count,
                    unit="个",
                ),
                MetricItem(
                    key="earliest_forecast_origin",
                    label="最早预测起点（UTC）",
                    value=_utc_text(min(origins)),
                ),
                MetricItem(
                    key="latest_forecast_origin",
                    label="最晚预测起点（UTC）",
                    value=_utc_text(max(origins)),
                ),
                MetricItem(
                    key="max_forecast_horizon_seconds",
                    label="最大预测时域",
                    value=max(sample.time_offsets_seconds[-1] for sample in samples),
                    unit="s",
                ),
            ],
        ),
    ]
    if output.excluded_counts:
        blocks.append(
            TableBlock(
                title="样本排除统计",
                columns=[
                    TableColumn(key="reason", label="原因"),
                    TableColumn(key="count", label="数量", unit="个"),
                ],
                rows=[
                    {"reason": reason, "count": count}
                    for reason, count in sorted(output.excluded_counts.items())
                ],
            )
        )

    # M7-04 persists a deterministic, bounded prefix of valid samples as
    # switchable snapshots. Every path is copied from ComparisonTaskOutput on
    # its frozen shared axis; the frontend never derives coordinates from metrics.
    visualization_samples = samples[:20]
    path_count = sum(1 + len(sample.method_results) for sample in visualization_samples)
    points_per_path = max(2, 5_000 // path_count)
    trajectory_rows: list[dict[str, Any]] = []
    snapshot_metric_rows: list[dict[str, Any]] = []

    def append_path(
        sample: ComparisonSampleResult,
        path_id: str,
        segment: str,
        points: tuple[ComparisonTrajectoryPoint, ...],
        horizontal_errors_m: tuple[float, ...] = (),
    ) -> None:
        selected_offsets = sample.time_offsets_seconds[:points_per_path]
        selected_points = points[:points_per_path]
        ground_speeds_mps: list[float | None] = [None] * len(selected_points)
        if len(selected_points) >= 2:
            interval_speeds = [
                haversine_metres(
                    previous.latitude_deg,
                    previous.longitude_deg,
                    current.latitude_deg,
                    current.longitude_deg,
                )
                / (current_offset - previous_offset)
                for previous_offset, current_offset, previous, current in zip(
                    selected_offsets[:-1],
                    selected_offsets[1:],
                    selected_points[:-1],
                    selected_points[1:],
                    strict=True,
                )
            ]
            # Display-only horizontal ground speed. The first point uses the
            # first interval's one-sided difference; later points use their
            # preceding interval. This never participates in ADE/FDE metrics.
            ground_speeds_mps = [interval_speeds[0], *interval_speeds]
        selected_errors = horizontal_errors_m[:points_per_path]
        for index, (offset, point, ground_speed_mps) in enumerate(
            zip(
                selected_offsets,
                selected_points,
                ground_speeds_mps,
                strict=True,
            )
        ):
            row: dict[str, Any] = {
                "longitude": point.longitude_deg,
                "latitude": point.latitude_deg,
                "time_step_s": offset,
                "trajectory_id": path_id,
                "segment": segment,
                "snapshot_id": (
                    f"{sample.input_ref.external_id} · {_utc_text(sample.forecast_origin)}"
                ),
            }
            if point.altitude_m is not None:
                row["altitude_m"] = point.altitude_m
            if ground_speed_mps is not None:
                row["ground_speed_mps"] = ground_speed_mps
            if index < len(selected_errors):
                row["horizontal_error_m"] = selected_errors[index]
            trajectory_rows.append(row)

    for sample in visualization_samples:
        append_path(sample, "actual", "actual_future", sample.actual_points)
        for method in sample.method_results:
            point_errors = method.horizontal_errors_metres
            if not point_errors:
                # Backward-compatible read path for comparison outputs persisted
                # before M7-07. Uses the same horizontal metric as ADE/FDE.
                point_errors = tuple(
                    haversine_metres(
                        predicted.latitude_deg,
                        predicted.longitude_deg,
                        actual.latitude_deg,
                        actual.longitude_deg,
                    )
                    for predicted, actual in zip(
                        method.predicted_points,
                        sample.actual_points,
                        strict=True,
                    )
                )
            append_path(
                sample,
                method.method_id,
                "predicted",
                method.predicted_points,
                point_errors,
            )
            worst_index = max(range(len(point_errors)), key=point_errors.__getitem__)
            snapshot_metric_rows.append(
                {
                    "snapshot_id": (
                        f"{sample.input_ref.external_id} · {_utc_text(sample.forecast_origin)}"
                    ),
                    "method_id": method.method_id,
                    "ade_m": method.ade_metres,
                    "fde_m": method.fde_metres,
                    "max_error_m": point_errors[worst_index],
                    "worst_time_s": sample.time_offsets_seconds[worst_index],
                }
            )
    blocks.append(
        TableBlock(
            title="可视化快照逐方法指标",
            columns=[
                TableColumn(key="snapshot_id", label="快照"),
                TableColumn(key="method_id", label="方法 ID"),
                TableColumn(key="ade_m", label="ADE", unit="m"),
                TableColumn(key="fde_m", label="FDE", unit="m"),
                TableColumn(key="max_error_m", label="最大水平误差", unit="m"),
                TableColumn(key="worst_time_s", label="最大误差时刻", unit="s"),
            ],
            rows=snapshot_metric_rows,
        )
    )
    trajectory = _trajectory_2d_block(
        trajectory_rows,
        x_key="longitude",
        y_key="latitude",
        x_label="经度",
        y_label="纬度",
        x_unit="degree",
        y_unit="degree",
        time_key="time_step_s",
        title="实际轨迹与多方法预测快照",
        series_specs=[
            ("altitude_m", "高度", "m"),
            ("ground_speed_mps", "水平地速", "m/s"),
            ("horizontal_error_m", "水平误差", "m"),
        ],
        path_key="trajectory_id",
        path_label="轨迹方法",
        segment_key="segment",
        segment_label="轨迹类型",
        snapshot_key="snapshot_id",
        snapshot_label="航班与预测起点",
    )
    if trajectory is not None:
        blocks.append(trajectory)

    if artifact_id is not None:
        base_url = visualization_base_url.rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("visualization_base_url must be http(s) or the same-origin root")
        url = f"{base_url}/visualizations/{quote(artifact_id, safe='')}/3d"
        if thread_id:
            url = f"{url}?{urlencode({'thread_id': thread_id})}"
        blocks.append(
            ActionLinkBlock(
                label="打开多模型三维轨迹对比",
                url=url,
                kind="visualization_3d",
                artifact_id=artifact_id,
                thread_id=thread_id,
            )
        )

    visualization_sample = visualization_samples[0]
    first_ref = visualization_sample.input_ref
    return AnalysisResult(
        schema_version="1.6",
        dataset="scat",
        operation="run_model_comparison",
        title=f"{output.model_key}:{output.model_version} 统一预测对比结果",
        start_time=_utc_text(visualization_sample.forecast_origin),
        context={
            "batch": first_ref.batch,
            "external_id": first_ref.external_id,
            "visualization_sample_id": visualization_sample.sample_id,
            "visualization_snapshot_count": len(visualization_samples),
            "visualization_snapshot_total": len(samples),
            "visualization_sample_policy": "first-20-valid-samples",
            "model_key": output.model_key,
            "model_version": output.model_version,
            "protocol_version": output.protocol_version,
            "sample_set_sha256": output.sample_set_sha256,
        },
        blocks=blocks,
    )


def build_scat_quality_problem_flights_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build metrics and a problem-flight table for the quality listing tool."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    rows = _rows(
        data.get("items"),
        (
            "original_filename",
            "trajectory_key",
            "external_id",
            "batch",
            "callsign",
            "point_count",
            "missing_point_count",
            "outlier_point_count",
            "spike_value_count",
        ),
    )
    query = _mapping(result.get("query"))
    scope = f"批次 {query['batch']}" if query.get("batch") else "全部SCAT航班"
    problem_type = query.get("problem_type")
    problem_label = {
        "missing": "含缺失值",
        "outlier": "含越界值",
        "spike": "含跳变值",
    }.get(problem_type if isinstance(problem_type, str) else "", "数据质量问题")
    return AnalysisResult(
        dataset="scat",
        operation="list_scat_quality_problem_flights",
        title=f"SCAT {problem_label}航班（{scope}）",
        context=_context(result),
        blocks=[
            MetricsBlock(
                title="问题航班统计",
                items=[
                    MetricItem(
                        key="total",
                        label=f"{problem_label}航班总数",
                        value=int(data.get("total") or 0),
                        unit="条",
                    ),
                    MetricItem(
                        key="returned",
                        label="本页返回",
                        value=int(data.get("returned") or 0),
                        unit="条",
                    ),
                    MetricItem(
                        key="has_more",
                        label="是否还有下一页",
                        value=bool(data.get("has_more")),
                    ),
                ],
            ),
            TableBlock(
                title="问题航班列表",
                columns=[
                    TableColumn(key="original_filename", label="原始文件名"),
                    TableColumn(key="trajectory_key", label="轨迹标识"),
                    TableColumn(key="external_id", label="航班号"),
                    TableColumn(key="batch", label="批次"),
                    TableColumn(key="callsign", label="呼号"),
                    TableColumn(key="point_count", label="轨迹点数", unit="个"),
                    TableColumn(
                        key="missing_point_count",
                        label="缺失点数",
                        unit="个",
                    ),
                    TableColumn(
                        key="outlier_point_count",
                        label="越界点数",
                        unit="个",
                    ),
                    TableColumn(
                        key="spike_value_count",
                        label="跳变值数",
                        unit="个",
                    ),
                ],
                rows=rows,
            ),
        ],
    )


def build_scat_quality_clean_flights_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build metrics and an identifier table for clean SCAT trajectories."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    rows = _rows(
        data.get("items"),
        (
            "original_filename",
            "trajectory_key",
            "external_id",
            "batch",
            "callsign",
            "point_count",
        ),
    )
    query = _mapping(result.get("query"))
    scope = f"批次 {query['batch']}" if query.get("batch") else "全部SCAT航班"
    return AnalysisResult(
        dataset="scat",
        operation="list_scat_quality_clean_flights",
        title=f"SCAT 无质量问题轨迹（{scope}）",
        context=_context(result),
        blocks=[
            MetricsBlock(
                title="无质量问题轨迹统计",
                items=[
                    MetricItem(
                        key="total",
                        label="符合当前口径的轨迹总数",
                        value=int(data.get("total") or 0),
                        unit="条",
                    ),
                    MetricItem(
                        key="returned",
                        label="本页返回",
                        value=int(data.get("returned") or 0),
                        unit="条",
                    ),
                    MetricItem(
                        key="has_more",
                        label="是否还有下一页",
                        value=bool(data.get("has_more")),
                    ),
                ],
            ),
            TableBlock(
                title="轨迹标识与原始文件名",
                columns=[
                    TableColumn(key="original_filename", label="原始文件名"),
                    TableColumn(key="trajectory_key", label="轨迹唯一标识"),
                    TableColumn(key="external_id", label="外部编号"),
                    TableColumn(key="batch", label="批次"),
                    TableColumn(key="callsign", label="呼号"),
                    TableColumn(key="point_count", label="轨迹点数", unit="个"),
                ],
                rows=rows,
            ),
        ],
    )


def build_uav_channel_stats_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build a per-channel cross-flight statistics table for UAV data."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    rows = _rows(
        data.get("items"),
        (
            "label",
            "channel",
            "unit",
            "flight_count",
            "total_samples",
            "weighted_mean",
            "min",
            "max",
            "missing_total",
        ),
    )
    query = _mapping(result.get("query"))
    filters: list[str] = []
    if query.get("model"):
        filters.append(f"机型 {query['model']}")
    if query.get("flight_kind"):
        filters.append(str(query["flight_kind"]))
    scope = " / ".join(filters) if filters else "全部架次"
    return AnalysisResult(
        dataset="uav",
        operation="get_uav_channel_stats",
        title=f"UAV 通道跨架次统计（{scope}）",
        context=_context(result),
        blocks=[
            MetricsBlock(
                title="统计范围",
                items=[
                    MetricItem(
                        key="flights_included",
                        label="参与统计架次",
                        value=int(data.get("flights_included") or 0),
                        unit="架次",
                    ),
                    MetricItem(
                        key="channel_count",
                        label="通道数",
                        value=len(rows),
                        unit="个",
                    ),
                ],
            ),
            TableBlock(
                title="通道统计",
                columns=[
                    TableColumn(key="label", label="通道"),
                    TableColumn(key="unit", label="单位"),
                    TableColumn(key="flight_count", label="架次数", unit="架次"),
                    TableColumn(key="total_samples", label="样本总数", unit="个"),
                    TableColumn(key="weighted_mean", label="加权均值", unit=None),
                    TableColumn(key="min", label="最小值", unit=None),
                    TableColumn(key="max", label="最大值", unit=None),
                    TableColumn(key="missing_total", label="缺失总数", unit="个"),
                ],
                rows=rows,
            ),
        ],
    )


def build_uav_normal_vs_anomaly_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build a normal-vs-anomaly channel comparison table for UAV data."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    items = data.get("items") if isinstance(data.get("items"), list) else []
    rows: list[dict[str, Any]] = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        normal = _mapping(item.get("normal"))
        anomaly = _mapping(item.get("anomaly"))
        rows.append(
            {
                "label": item.get("label"),
                "unit": item.get("unit"),
                "normal_mean": normal.get("weighted_mean"),
                "normal_min": normal.get("min"),
                "normal_max": normal.get("max"),
                "normal_flights": normal.get("flight_count"),
                "anomaly_mean": anomaly.get("weighted_mean"),
                "anomaly_min": anomaly.get("min"),
                "anomaly_max": anomaly.get("max"),
                "anomaly_flights": anomaly.get("flight_count"),
                "delta_mean": item.get("delta_mean"),
                "delta_relative": item.get("delta_relative"),
            }
        )
    query = _mapping(result.get("query"))
    scope = f"机型 {query['model']}" if query.get("model") else "全部机型"
    return AnalysisResult(
        dataset="uav",
        operation="get_uav_normal_vs_anomaly_stats",
        title=f"UAV 正常 vs 异常通道对比（{scope}）",
        context=_context(result),
        blocks=[
            TableBlock(
                title="正常 vs 异常样本通道统计",
                columns=[
                    TableColumn(key="label", label="通道"),
                    TableColumn(key="unit", label="单位"),
                    TableColumn(key="normal_mean", label="正常均值", unit=None),
                    TableColumn(key="normal_min", label="正常最小", unit=None),
                    TableColumn(key="normal_max", label="正常最大", unit=None),
                    TableColumn(key="normal_flights", label="正常架次", unit="架次"),
                    TableColumn(key="anomaly_mean", label="异常均值", unit=None),
                    TableColumn(key="anomaly_min", label="异常最小", unit=None),
                    TableColumn(key="anomaly_max", label="异常最大", unit=None),
                    TableColumn(key="anomaly_flights", label="异常架次", unit="架次"),
                    TableColumn(key="delta_mean", label="均值差(异常-正常)", unit=None),
                    TableColumn(key="delta_relative", label="相对差", unit=None),
                ],
                rows=rows,
            ),
        ],
    )


def build_uav_flight_phase_stats_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build a climb/level/descent phase table for one UAV flight."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    rows: list[dict[str, Any]] = []
    for phase in (data.get("phases") or [])[:10]:
        if not isinstance(phase, dict):
            continue
        altitude = _mapping(phase.get("altitude"))
        ground_speed = _mapping(phase.get("ground_speed"))
        vertical_speed = _mapping(phase.get("vertical_speed"))
        rows.append(
            {
                "phase": phase.get("phase"),
                "point_count": phase.get("point_count"),
                "point_ratio": phase.get("point_ratio"),
                "duration_seconds": phase.get("duration_seconds"),
                "min_altitude_m": altitude.get("minimum_m"),
                "average_altitude_m": altitude.get("average_m"),
                "max_altitude_m": altitude.get("maximum_m"),
                "average_ground_speed_mps": ground_speed.get("average_mps"),
                "maximum_ground_speed_mps": ground_speed.get("maximum_mps"),
                "average_vertical_speed_mps": vertical_speed.get("average_mps"),
            }
        )
    external_id = str(result.get("query", {}).get("external_id") or "UAV")
    return AnalysisResult(
        dataset="uav",
        operation="get_uav_phase_stats",
        title=f"UAV 架次 {external_id} 飞行阶段统计",
        context=_context(result),
        blocks=[
            MetricsBlock(
                title="架次概况",
                items=[
                    MetricItem(
                        key="total_points",
                        label="样本总数",
                        value=int(data.get("total_points") or 0),
                        unit="个",
                    ),
                    MetricItem(
                        key="phase_count",
                        label="阶段数",
                        value=len(rows),
                        unit="个",
                    ),
                    MetricItem(
                        key="phase_threshold",
                        label="阶段划分阈值(天向速度)",
                        value=float(data.get("phase_threshold_mps") or 1.0),
                        unit="m/s",
                    ),
                ],
            ),
            TableBlock(
                title="飞行阶段统计",
                columns=[
                    TableColumn(key="phase", label="阶段"),
                    TableColumn(key="point_count", label="样本数", unit="个"),
                    TableColumn(key="point_ratio", label="样本占比", unit=None),
                    TableColumn(key="duration_seconds", label="持续时长", unit="s"),
                    TableColumn(key="min_altitude_m", label="最低高度", unit="m"),
                    TableColumn(key="average_altitude_m", label="平均高度", unit="m"),
                    TableColumn(key="max_altitude_m", label="最高高度", unit="m"),
                    TableColumn(key="average_ground_speed_mps", label="平均地速", unit="m/s"),
                    TableColumn(key="maximum_ground_speed_mps", label="最大地速", unit="m/s"),
                    TableColumn(key="average_vertical_speed_mps", label="平均天向速度", unit="m/s"),
                ],
                rows=rows,
            ),
        ],
    )


def build_scat_airport_stats_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Build metrics and a delay table for per-airport departure statistics."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    statistics = _mapping(data.get("statistics"))
    metric_specs = (
        ("flight_count", "航班总数", "条"),
        ("airport_count", "起飞机场数", "个"),
        ("with_atd_count", "有实际起飞时间航班", "条"),
        ("mean_delay_minutes", "平均起飞延误", "min"),
        ("delayed_count", "延误≥15分钟航班", "条"),
        ("delayed_rate", "延误率", None),
    )
    metrics = [
        item
        for key, label, unit in metric_specs
        if (item := _metric(statistics, key, label, unit)) is not None
    ]
    rows = _rows(
        data.get("airports"),
        (
            "airport",
            "flight_count",
            "with_atd_count",
            "mean_delay_minutes",
            "max_delay_minutes",
            "min_delay_minutes",
            "delayed_count",
            "delayed_rate",
        ),
    )
    batch = str(result.get("query", {}).get("batch") or "SCAT")
    return AnalysisResult(
        dataset="scat",
        operation="get_scat_airport_stats",
        title=f"SCAT 批次 {batch} 机场起飞统计",
        context=_context(result),
        blocks=[
            MetricsBlock(title="批次延误概况", items=metrics),
            TableBlock(
                title="各机场航班量与起飞延误",
                columns=[
                    TableColumn(key="airport", label="起飞机场"),
                    TableColumn(key="flight_count", label="航班数", unit="条"),
                    TableColumn(key="with_atd_count", label="有ATD航班", unit="条"),
                    TableColumn(key="mean_delay_minutes", label="平均延误", unit="min"),
                    TableColumn(key="max_delay_minutes", label="最大延误", unit="min"),
                    TableColumn(key="min_delay_minutes", label="最小延误", unit="min"),
                    TableColumn(key="delayed_count", label="延误≥15min", unit="条"),
                    TableColumn(key="delayed_rate", label="延误率", unit=None),
                ],
                rows=rows,
            ),
        ],
    )


def build_scat_flight_phase_stats_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build a phase table for a single SCAT flight."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    rows: list[dict[str, Any]] = []
    for phase in (data.get("phases") or [])[:10]:
        if not isinstance(phase, dict):
            continue
        flight_level = _mapping(phase.get("flight_level"))
        ground_speed = _mapping(phase.get("ground_speed"))
        vertical_rate = _mapping(phase.get("absolute_vertical_rate"))
        rows.append(
            {
                "phase_name": phase.get("phase_name"),
                "point_count": phase.get("point_count"),
                "point_ratio": phase.get("point_ratio"),
                "duration_seconds": phase.get("duration_seconds"),
                "min_flight_level": flight_level.get("minimum"),
                "average_flight_level": flight_level.get("average"),
                "max_flight_level": flight_level.get("maximum"),
                "average_ground_speed_mps": ground_speed.get("average_mps"),
                "maximum_ground_speed_mps": ground_speed.get("maximum_mps"),
                "average_abs_rocd_fpm": vertical_rate.get("average_fpm"),
                "maximum_abs_rocd_fpm": vertical_rate.get("maximum_fpm"),
            }
        )
    external_id = str(result.get("query", {}).get("external_id") or "SCAT")
    return AnalysisResult(
        dataset="scat",
        operation="get_scat_flight_phase_stats",
        title=f"SCAT 航班 {external_id} 飞行阶段统计",
        context=_context(result),
        blocks=[
            MetricsBlock(
                title="轨迹概况",
                items=[
                    MetricItem(
                        key="total_points",
                        label="轨迹总点数",
                        value=int(data.get("total_points") or 0),
                        unit="个",
                    ),
                    MetricItem(
                        key="phase_count",
                        label="阶段数",
                        value=len(rows),
                        unit="个",
                    ),
                ],
            ),
            TableBlock(
                title="飞行阶段统计",
                columns=[
                    TableColumn(key="phase_name", label="阶段"),
                    TableColumn(key="point_count", label="轨迹点数", unit="个"),
                    TableColumn(key="point_ratio", label="点数占比", unit=None),
                    TableColumn(key="duration_seconds", label="持续时长", unit="s"),
                    TableColumn(key="min_flight_level", label="最低高度层", unit=None),
                    TableColumn(key="average_flight_level", label="平均高度层", unit=None),
                    TableColumn(key="max_flight_level", label="最高高度层", unit=None),
                    TableColumn(key="average_ground_speed_mps", label="平均地速", unit="m/s"),
                    TableColumn(key="maximum_ground_speed_mps", label="最大地速", unit="m/s"),
                    TableColumn(key="average_abs_rocd_fpm", label="平均升降率", unit="ft/min"),
                    TableColumn(key="maximum_abs_rocd_fpm", label="最大升降率", unit="ft/min"),
                ],
                rows=rows,
            ),
        ],
    )


def build_scat_prediction_error_analysis(
    result: dict[str, Any],
) -> AnalysisResult | None:
    """Build error metrics and a per-fix table for SCAT prediction errors."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None

    data = result["data"]
    statistics = _mapping(data.get("statistics"))
    horizontal = _mapping(statistics.get("horizontal_error"))
    vertical = _mapping(statistics.get("vertical_error"))
    time_error = _mapping(statistics.get("time_error"))
    metrics = [
        item
        for key, label, unit in (
            ("mean_m", "水平误差均值", "m"),
            ("median_m", "水平误差中位数", "m"),
            ("max_m", "水平误差最大值", "m"),
            ("rmse_m", "水平误差 RMSE", "m"),
        )
        if (item := _metric(horizontal, key, label, unit)) is not None
    ]
    metrics.extend(
        item
        for key, label, unit in (
            ("mean_abs_m", "垂直误差均值", "m"),
            ("max_abs_m", "垂直误差最大值", "m"),
        )
        if (item := _metric(vertical, key, label, unit)) is not None
    )
    metrics.extend(
        item
        for key, label, unit in (
            ("mean_abs_s", "时间误差均值", "s"),
            ("mean_signed_s", "时间误差均值(带符号)", "s"),
            ("max_abs_s", "时间误差最大值", "s"),
        )
        if (item := _metric(time_error, key, label, unit)) is not None
    )
    metrics.extend(
        [
            MetricItem(
                key="matched_fix_count",
                label="匹配航路点数",
                value=int(data.get("matched_fix_count") or 0),
                unit="个",
            ),
            MetricItem(
                key="unmatched_fix_count",
                label="未匹配航路点数",
                value=int(data.get("unmatched_fix_count") or 0),
                unit="个",
            ),
        ]
    )
    fixes = _rows(
        data.get("fixes"),
        (
            "fix_name",
            "fix_kind",
            "eto",
            "time_error_s",
            "horizontal_error_m",
            "vertical_error_m",
        ),
    )
    external_id = str(result.get("query", {}).get("external_id") or "SCAT")
    return AnalysisResult(
        dataset="scat",
        operation="get_scat_prediction_error_stats",
        title=f"SCAT 航班 {external_id} 预测误差统计",
        context=_context(result),
        blocks=[
            MetricsBlock(title="预测误差指标", items=metrics),
            TableBlock(
                title="航路点误差明细",
                columns=[
                    TableColumn(key="fix_name", label="航路点"),
                    TableColumn(key="fix_kind", label="类型"),
                    TableColumn(key="eto", label="预计过点时间"),
                    TableColumn(key="time_error_s", label="时间误差", unit="s"),
                    TableColumn(key="horizontal_error_m", label="水平误差", unit="m"),
                    TableColumn(key="vertical_error_m", label="垂直误差", unit="m"),
                ],
                rows=fixes,
            ),
        ],
    )


def build_cfpl_materialized_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Project only the fetched materialized CFPL page; never invent global metrics."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None
    data = result["data"]
    summaries = result.get("operation") == "get_prediction_metric_summaries"
    fields = (
        [
            ("group_by", "分组维度", None),
            ("group_value", "分组", None),
            ("horizon_s", "预测时域", "s"),
            ("sample_count", "样本量", None),
            ("flight_count", "航班数", None),
            ("horizontal_mae_m", "水平 MAE", "m"),
            ("horizontal_rmse_m", "水平 RMSE", "m"),
            ("vertical_mae_m", "垂直 MAE", "m"),
            ("vertical_rmse_m", "垂直 RMSE", "m"),
            ("horizontal_p95_m", "水平 P95", "m"),
        ]
        if summaries
        else [
            ("external_id", "航班", None),
            ("snapshot_time", "预测时间", None),
            ("horizon_s", "预测时域", "s"),
            ("horizontal_error_m", "水平误差", "m"),
            ("vertical_error_m", "垂直误差", "m"),
            ("along_track_error_m", "沿航向误差", "m"),
            ("cross_track_error_m", "横向误差", "m"),
            ("dominant_phase", "主导阶段", None),
            ("aircraft_type", "机型", None),
        ]
    )
    rows = _rows(data.get("summaries" if summaries else "samples"), tuple(f[0] for f in fields))
    return AnalysisResult(
        dataset="scat",
        operation=result["operation"],
        title="CFPL 已物化指标汇总" if summaries else "CFPL 已物化最差样本",
        context={
            **_context(result),
            **{"page_" + key: value for key, value in data.get("pagination", {}).items()},
            "metric_protocol": data.get("metric_protocol"),
            "coverage_note": data.get("coverage_note"),
            "comparison": data.get("comparison"),
        },
        blocks=[
            MetricsBlock(
                title="本页范围与口径",
                items=[
                    MetricItem(key="returned", label="本页行数", value=len(rows)),
                    MetricItem(key="comparison", label="比较对象", value="CFPL计划 vs 实飞"),
                    MetricItem(
                        key="coverage",
                        label="覆盖边界",
                        value=str(data.get("coverage_note", "仅已有物化样本")),
                    ),
                ],
            ),
            TableBlock(
                title="CFPL 指标（本页）" if summaries else "CFPL 最差样本（本页）",
                columns=[TableColumn(key=k, label=l, unit=u) for k, l, u in fields],
                rows=rows,
            ),
        ],
    )


def build_scat_model_quality_analysis(result: dict[str, Any]) -> AnalysisResult | None:
    """Display the inspected source window; readiness is not model accuracy."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        return None
    data = result["data"]
    if not isinstance(data.get("model_ready"), bool):
        return None
    quality = _mapping(data.get("quality"))
    items = [MetricItem(key="model_ready", label="输入窗口可供模型使用", value=data["model_ready"])]
    for key, label in (
        ("source_points_received", "检查源点数"),
        ("repaired_values", "修复值数"),
        ("unrepaired_values", "未修复值数"),
    ):
        item = _metric(quality, key, label, "个")
        if item is not None:
            items.append(item)
    if isinstance(data.get("error_code"), str):
        items.append(MetricItem(key="error_code", label="不可用原因", value=data["error_code"]))
    keys = (
        "feature",
        "missing_or_non_finite",
        "out_of_range",
        "spike",
        "interpolated",
        "extrapolated",
        "unrepaired",
    )
    return AnalysisResult(
        dataset="scat",
        operation="inspect_scat_model_data_quality",
        title="SCAT 模型输入窗口质量检查",
        context=_context(result),
        blocks=[
            MetricsBlock(title="窗口检查（不执行预测）", items=items),
            TableBlock(
                title="特征质量明细",
                columns=[
                    TableColumn(key=k, label=label)
                    for k, label in zip(
                        keys,
                        (
                            "特征",
                            "缺失或非有限值",
                            "越界值",
                            "突变值",
                            "插值修复值",
                            "外推修复值",
                            "未修复值",
                        ),
                        strict=True,
                    )
                ],
                rows=_rows(quality.get("features"), keys),
            ),
        ],
    )


__all__ = [
    "attach_analysis_result",
    "build_comparison_model_analysis",
    "build_regression_model_analysis",
    "build_scat_airport_stats_analysis",
    "build_scat_batch_analysis",
    "build_scat_flight_overview_analysis",
    "build_scat_flight_phase_stats_analysis",
    "build_scat_flight_search_analysis",
    "build_scat_model_quality_analysis",
    "build_scat_prediction_error_analysis",
    "build_scat_quality_clean_flights_analysis",
    "build_scat_quality_problem_flights_analysis",
    "build_scat_track_analysis",
    "build_uav_channel_stats_analysis",
    "build_uav_flight_overview_analysis",
    "build_uav_flight_phase_stats_analysis",
    "build_uav_flight_search_analysis",
    "build_uav_model_analysis",
    "build_uav_normal_vs_anomaly_analysis",
    "build_uav_samples_analysis",
]
