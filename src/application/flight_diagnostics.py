"""Bounded diagnostics over existing read-only flight data and its declared units."""

from collections import Counter
from itertools import product
from math import isfinite
from statistics import StatisticsError, correlation

from agents.analysis_results import (
    _time_series_blocks,
    _utc_text,
    build_scat_track_analysis,
    build_uav_samples_analysis,
)
from scat.prediction.alignment import haversine_metres, parse_iso_utc
from schema.analysis import AnalysisResult, MetricItem, MetricsBlock, TableBlock, TableColumn
from uav.schemas.channels import CHANNEL_BY_KEY

TRACK_EVENT_LIMIT = 60


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _table(title, fields, rows):
    return TableBlock(
        title=title,
        columns=[TableColumn(key=key, label=label, unit=unit) for key, label, unit in fields],
        rows=rows,
    )


def _metrics(title, items):
    return MetricsBlock(
        title=title,
        items=[MetricItem(key=key, label=label, value=value) for key, label, value in items],
    )


def _box_table(title, unit, rows):
    fields = [("group", "分组", None), ("valid", "有效样本数", None)]
    fields.extend(
        ("box_" + key, label, unit)
        for key, label in zip(
            ("min", "q1", "median", "q3", "max"),
            ("最小值", "P25", "P50", "P75", "最大值"),
            strict=True,
        )
    )
    return _table(
        title,
        fields,
        [
            {
                "group": label,
                "valid": count,
                **{"box_" + key: stats[key] for key in ("min", "q1", "median", "q3", "max")},
            }
            for label, count, stats in rows
        ],
    )


def _result(operation, source, analysis, blocks, evidence):
    analysis.operation = operation
    analysis.blocks = [*blocks, *analysis.blocks]
    return {
        "status": "ok",
        "operation": operation,
        "query": source.get("query", {}),
        "data": evidence,
        "analysis_result": analysis.model_dump(mode="json"),
    }


def _quantile(ordered, fraction):
    if not ordered:
        return None
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (
        index - lower
    )


def _histogram(title, unit, values):
    low, high = min(values), max(values)
    bins = 1 if low == high else 10
    step = (high - low) / bins
    counts = [0] * bins
    for value in values:
        counts[min(int((value - low) / step), bins - 1) if step else 0] += 1
    return _table(
        title,
        [
            ("bin", "区间", unit),
            ("lower", "下界", unit),
            ("upper", "上界", unit),
            ("count", "样本数", None),
        ],
        [
            {
                "bin": f"{low + i * step:.6g}–{high if i == bins - 1 else low + (i + 1) * step:.6g}",
                "lower": low + i * step,
                "upper": high if i == bins - 1 else low + (i + 1) * step,
                "count": count,
            }
            for i, count in enumerate(counts)
        ],
    )


def _phase(value):
    if value is None:
        return "未知"
    return "爬升" if value > 1 else "下降" if value < -1 else "平飞"


def uav_channels(source, channels):
    if source.get("status") != "ok":
        return source
    data = source["data"]
    samples = data["samples"]
    times = [_number(row.get("t_s")) for row in samples]
    counts = Counter(times)
    unique = [
        row for row, t in zip(samples, times, strict=True) if t is not None and counts[t] == 1
    ]
    unique.sort(key=lambda row: row["t_s"])
    # Phase metadata may be loaded internally; only the requested channels appear in charts.
    clean_samples = [{key: row[key] for key in ("t_s", *channels) if key in row} for row in unique]
    clean = {**source, "data": {**data, "samples": clean_samples}}
    analysis = build_uav_samples_analysis(clean)
    if analysis.start_time is None and unique:
        # Some real source files have elapsed seconds but no calendar start time.
        # AnalysisResult 1.1 represents relative time without fabricating a UTC date.
        analysis = AnalysisResult(
            dataset="uav",
            operation=analysis.operation,
            title=analysis.title,
            schema_version="1.1",
            context={**analysis.context, "time_basis": "recorded_elapsed_seconds"},
            blocks=[
                *(block for block in analysis.blocks if block.type == "metrics"),
                *_time_series_blocks(
                    clean_samples,
                    x_key="t_s",
                    x_label="架次记录的相对秒",
                    x_type="number",
                    x_unit="s",
                    title_prefix="UAV 相对时序",
                    series_specs=[
                        (key, CHANNEL_BY_KEY[key].label, CHANNEL_BY_KEY[key].unit)
                        for key in channels
                    ],
                ),
            ],
        )
    by_phase = {
        "全窗口": unique,
        **{
            phase: [row for row in unique if _phase(_number(row.get("cmp_comb_vu"))) == phase]
            for phase in ("爬升", "平飞", "下降", "未知")
        },
    }
    distribution = []
    for key, phase in product(channels, by_phase):
        rows = by_phase[phase]
        if not rows and phase != "全窗口":
            continue
        values = sorted(v for row in rows if (v := _number(row.get(key))) is not None)
        distribution.append(
            {
                "channel": key,
                "phase": phase,
                "unit": CHANNEL_BY_KEY[key].unit,
                "window_count": len(rows),
                "valid": len(values),
                "missing_percent": 100 * (len(rows) - len(values)) / len(rows) if rows else None,
                **{
                    name: _quantile(values, q)
                    for name, q in [
                        ("min", 0),
                        ("q1", 0.25),
                        ("median", 0.5),
                        ("q3", 0.75),
                        ("max", 1),
                    ]
                },
            }
        )
    labels = {key: f"{CHANNEL_BY_KEY[key].label} ({key})" for key in channels}
    correlations = []
    for left, right in product(channels, repeat=2):
        pairs = [
            (a, b)
            for row in unique
            if (a := _number(row.get(left))) is not None
            and (b := _number(row.get(right))) is not None
        ]
        value = None
        if len(pairs) >= 3:
            a, b = zip(*pairs, strict=True)
            try:
                value = correlation(a, b)
            except StatisticsError:
                pass
        correlations.append(
            {
                "channel_x": labels[left],
                "channel_y": labels[right],
                "pearson_r": value,
                "paired_count": len(pairs),
            }
        )
    coverage = {
        "source_points": len(samples),
        "aligned_points": len(unique),
        "invalid_time": counts[None],
        "duplicate_time_rows": sum(n for t, n in counts.items() if t is not None and n > 1),
        "start": data["start"],
        "next_start": data["next_start"],
        "has_more": data["has_more"],
    }
    blocks = [
        _metrics(
            "原始窗口与时间对齐",
            [
                ("source", "源窗口行数", len(samples)),
                ("aligned", "唯一时间行数", len(unique)),
                ("invalid", "无有效时间的行", counts[None]),
                ("duplicates", "被排除的重复时间行", coverage["duplicate_time_rows"]),
                ("more", "还有下一页", data["has_more"]),
            ],
        ),
        _table(
            "通道分布与阶段覆盖（按单位看数值）",
            [
                ("channel", "通道", None),
                ("phase", "阶段", None),
                ("unit", "数值单位", None),
                ("window_count", "阶段行数", None),
                ("valid", "有效值", None),
                ("missing_percent", "缺测率", "%"),
                ("min", "最小值", None),
                ("q1", "P25", None),
                ("median", "中位数", None),
                ("q3", "P75", None),
                ("max", "最大值", None),
            ],
            distribution,
        ),
        _table(
            "同期 Pearson 通道相关性（至少 3 对，常量不计算）",
            [
                ("channel_x", "通道 X", None),
                ("channel_y", "通道 Y", None),
                ("pearson_r", "Pearson r", None),
                ("paired_count", "配对样本数", None),
            ],
            correlations,
        ),
    ]
    for unit in dict.fromkeys(CHANNEL_BY_KEY[key].unit for key in channels):
        data_rows = [row for row in distribution if row["unit"] == unit and row["valid"]]
        if data_rows:
            blocks.append(
                _box_table(
                    f"阶段分布五数概括（{unit}）",
                    unit,
                    [
                        (f"{row['channel']} · {row['phase']}", row["valid"], row)
                        for row in data_rows
                    ],
                )
            )
    return _result(
        "diagnose_uav_channels",
        source,
        analysis,
        blocks,
        {**coverage, "correlations": correlations, "distribution": distribution},
    )


def _coords(point):
    position = point.get("position") or {}
    lat, lon, alt = (_number(position.get(key)) for key in ("lat_deg", "lon_deg", "altitude_m"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None, alt
    return lat, lon, alt


def track_quality(source, *, gap_s, max_ground_speed_mps, max_vertical_rate_mps):
    if source.get("status") != "ok":
        return source
    data = source["data"]
    points = []
    for point in data["points"]:
        time = parse_iso_utc(point.get("timestamp"))
        lat, lon, alt = _coords(point)
        points.append(
            {
                **point,
                "timestamp": _utc_text(time) if time else None,
                "position": {"lat_deg": lat, "lon_deg": lon, "altitude_m": alt},
            }
        )
    analysis = build_scat_track_analysis({**source, "data": {**data, "points": points}})
    origin = parse_iso_utc(analysis.start_time)
    events, intervals = [], []
    seen = set()
    previous = None
    for point in points:
        time = parse_iso_utc(point.get("timestamp"))
        lat, lon, alt = _coords(point)
        row = {
            "start_seq": point["sequence"],
            "end_seq": point["sequence"],
            "start": point.get("timestamp"),
            "end": point.get("timestamp"),
            "lat": lat,
            "lon": lon,
            "focus_time_s": (time - origin).total_seconds()
            if time and origin and lat is not None
            else None,
        }
        if time is None or lat is None:
            events.append({**row, "event": "时间或位置缺失"})
        if time is not None and time in seen:
            events.append({**row, "event": "重复时间戳"})
        if time is not None:
            seen.add(time)
        if previous and time is not None and previous[1] is not None:
            last, last_time, last_lat, last_lon, last_alt = previous
            dt = (time - last_time).total_seconds()
            section = {
                **row,
                "start_seq": last["sequence"],
                "start": last.get("timestamp"),
                "interval": dt,
            }
            if dt < 0:
                events.append({**section, "event": "时间逆序"})
            if dt > 0:
                intervals.append(dt)
                if dt > gap_s:
                    events.append({**section, "event": "采样间隔过长"})
                else:
                    if lat is not None and last_lat is not None:
                        rate = haversine_metres(last_lat, last_lon, lat, lon) / dt
                        if rate > max_ground_speed_mps:
                            events.append(
                                {**section, "event": "疑似位置跳变", "ground_speed": rate}
                            )
                    if alt is not None and last_alt is not None:
                        rate = (alt - last_alt) / dt
                        if abs(rate) > max_vertical_rate_mps:
                            events.append(
                                {**section, "event": "爬降速率超阈值", "vertical_rate": rate}
                            )
        previous = (point, time, lat, lon, alt)
    coverage = data["pagination"]
    summary = {
        "events": len(events),
        "event_counts": dict(Counter(row["event"] for row in events)),
        "returned_events": min(len(events), TRACK_EVENT_LIMIT),
        "pagination": coverage,
        "gap_s": gap_s,
        "max_ground_speed_mps": max_ground_speed_mps,
        "max_vertical_rate_mps": max_vertical_rate_mps,
    }
    eligible = sum(p["timestamp"] is not None and _coords(p)[0] is not None for p in points)
    blocks = [
        _metrics(
            "当前连续轨迹页诊断",
            [
                ("points", "本页轨迹点", len(points)),
                ("valid", "有时间和有效坐标的点", eligible),
                (
                    "valid_fraction",
                    "点可用率（%）",
                    100 * eligible / len(points) if points else None,
                ),
                ("events", "异常事件数", len(events)),
                ("shown", "展示事件数", summary["returned_events"]),
                ("gap", "间隔阈值（秒）", gap_s),
                ("speed", "位移速度阈值（m/s）", max_ground_speed_mps),
                ("vertical", "爬降阈值（m/s）", max_vertical_rate_mps),
            ],
        ),
        _table(
            f"事件明细（按源点顺序，最多 {TRACK_EVENT_LIMIT} 条）",
            [
                ("event", "事件", None),
                ("start_seq", "起点序号", None),
                ("end_seq", "终点序号", None),
                ("start", "起点时间", None),
                ("end", "终点时间", None),
                ("interval", "时间差", "s"),
                ("lat", "终点纬度", "deg"),
                ("lon", "终点经度", "deg"),
                ("ground_speed", "位移速度", "m/s"),
                ("vertical_rate", "估算爬降速率", "m/s"),
            ],
            events[:TRACK_EVENT_LIMIT],
        ),
    ]
    if intervals:
        blocks.append(_histogram("本页正采样间隔分布", "s", intervals))
    return _result("diagnose_scat_track", source, analysis, blocks, summary)


def prediction_errors(source, metric):
    if source.get("status") != "ok":
        return source
    data = source["data"]
    key, label, unit = {
        "horizontal": ("horizontal_error_m", "水平距离误差", "m"),
        "vertical": ("vertical_error_m", "垂直有符号误差", "m"),
        "time": ("time_error_s", "有符号时间误差", "s"),
    }[metric]
    fixes = data["fixes"]
    usable = [(v, row) for row in fixes if (v := _number(row.get(key))) is not None]
    ordered = sorted(value for value, _ in usable)
    matched = data["matched_fix_count"]
    summary = {
        "snapshot_time": data.get("snapshot_time"),
        "matched": matched,
        "unmatched": data["unmatched_fix_count"],
        "detail_rows": len(fixes),
        "usable": len(usable),
        "metric": metric,
        "unit": unit,
        "p50": _quantile(ordered, 0.5),
        "p95": _quantile(ordered, 0.95),
        "truncated": matched > len(fixes),
    }
    blocks = [
        _metrics(
            "已有快照匹配明细的统计",
            [
                ("source", "来源", "CFPL 已有快照与实飞"),
                ("matched", "匹配成功航路点", matched),
                ("unmatched", "未匹配航路点", summary["unmatched"]),
                ("detail", "本次可见匹配明细", len(fixes)),
                ("valid", "所选指标有效值", len(ordered)),
                ("median", f"明细 P50（{unit}）", summary["p50"]),
                ("p95", f"明细 P95（{unit}）", summary["p95"]),
                ("truncated", "匹配明细已截断", summary["truncated"]),
            ],
        )
    ]
    if ordered:
        blocks.append(_histogram(f"{label}直方图（匹配明细）", unit, ordered))
        blocks.append(
            _box_table(
                f"{label}五数概括（匹配明细）",
                unit,
                [
                    (
                        "匹配明细",
                        len(ordered),
                        {
                            name: _quantile(ordered, q)
                            for name, q in [
                                ("min", 0),
                                ("q1", 0.25),
                                ("median", 0.5),
                                ("q3", 0.75),
                                ("max", 1),
                            ]
                        },
                    )
                ],
            )
        )
        cdf = [(v, i) for i, v in enumerate(ordered, 1) if i == len(ordered) or ordered[i] != v]
        blocks.append(
            _table(
                f"{label}经验累计分布（匹配明细）",
                [("error", "误差", unit), ("cdf_percent", "累计比例", "%")],
                [
                    {"error": value, "cdf_percent": 100 * count / len(ordered)}
                    for value, count in cdf
                ],
            )
        )
    tail = sorted(usable, key=lambda item: abs(item[0]), reverse=True)[:10]
    blocks.append(
        _table(
            "绝对误差最大的可见匹配点",
            [
                ("fix_name", "航路点", None),
                ("eto", "计划时刻", None),
                ("matched_time", "匹配实飞时刻", None),
                ("error", label, unit),
            ],
            [
                {
                    "fix_name": row.get("fix_name"),
                    "eto": row.get("eto"),
                    "matched_time": row.get("matched_time"),
                    "error": value,
                }
                for value, row in tail
            ],
        )
    )
    analysis = AnalysisResult(
        dataset="scat",
        operation="diagnose_scat_prediction_errors",
        title=f"SCAT 已有预测误差分布 · {label}",
        blocks=blocks,
    )
    return _result(analysis.operation, source, analysis, [], summary)


def batch_comparison(results, batches, group):
    operation = "compare_scat_batch_profiles"
    summary, volume, groups = [], [], []
    group_name, item_key, group_label = {
        "aircraft": ("top_aircraft_types", "aircraft_type", "机型"),
        "departure": ("top_departure_airports", "airport", "起飞机场"),
        "arrival": ("top_arrival_airports", "airport", "到达机场"),
    }[group]
    for batch, source in zip(batches, results, strict=True):
        data = source.get("data") or {}
        counts, quality = data.get("statistics") or {}, data.get("data_quality") or {}
        total = counts.get("flight_count")
        empty = quality.get("empty_trajectory_count")
        daily = data.get("flight_volume", [])
        summary.append(
            {
                "batch": batch,
                "status": source["status"],
                "flights": total,
                "empty": empty,
                "empty_percent": 100 * empty / total if total and empty is not None else None,
                "scheduled_count": sum(row["flight_count"] for row in daily)
                if source["status"] == "ok"
                else None,
                "days_shown": min(len(daily), 50),
                "days_available": len(daily),
                "source": data.get("statistics_source"),
                "generated_at": data.get("statistics_generated_at"),
            }
        )
        volume.extend(
            {"batch": batch, "day": row["period_start"], "flights": row["flight_count"]}
            for row in daily[:50]
        )
        groups.extend(
            {
                "batch": batch,
                "group": row.get(item_key),
                "flights": row["flight_count"],
                "total": total,
                "percent": 100 * row["flight_count"] / total if total else None,
            }
            for row in data.get(group_name, [])
        )
    blocks = [
        _table(
            "所选批次及覆盖",
            [
                ("batch", "批次", None),
                ("status", "读取状态", None),
                ("flights", "批次航班数", None),
                ("empty", "空轨迹航班数", None),
                ("empty_percent", "空轨迹比例", "%"),
                ("scheduled_count", "有有效计划起飞时刻的航班数", None),
                ("days_shown", "展示日期数", None),
                ("days_available", "有效日期数", None),
                ("source", "统计来源", None),
                ("generated_at", "物化时间", None),
            ],
            summary,
        ),
        _table(
            "UTC 计划航班量（每批最多前 50 个有效日期）",
            [("day", "UTC 日期", None), ("batch", "批次", None), ("flights", "计划航班数", None)],
            volume,
        ),
        _table(
            f"每批前 10 {group_label}（缺席分组没有补零）",
            [
                ("group", group_label, None),
                ("batch", "批次", None),
                ("flights", "分组航班数", None),
                ("total", "批次航班总数", None),
                ("percent", "占批次比例", "%"),
            ],
            groups,
        ),
    ]
    analysis = AnalysisResult(
        dataset="scat", operation=operation, title="SCAT 批次趋势与结构对比", blocks=blocks
    )
    return _result(
        operation,
        {"query": {"batches": batches, "group": group}},
        analysis,
        [],
        {"batches": summary},
    )
