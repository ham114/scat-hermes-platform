"""Generic Streamlit renderer for AnalysisResult v1."""

from __future__ import annotations

import math
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

try:
    import pydeck as pdk
except ImportError:  # pragma: no cover - 仅影响地图底图选项
    pdk = None

from schema.analysis import (
    ActionLinkBlock,
    AnalysisResult,
    ClassificationBlock,
    MetricsBlock,
    PredictionBlock,
    TableBlock,
    TimeSeriesBlock,
    Trajectory2dBlock,
)

_trajectory_toggle_counter = 0


def _next_trajectory_widget_key() -> str:
    """每次渲染生成唯一且按渲染顺序稳定的 widget key，避免跨结果撞 key。"""
    global _trajectory_toggle_counter
    _trajectory_toggle_counter += 1
    return f"trajectory_2d_widget_{_trajectory_toggle_counter}"


# 底图选项：无底图用 Altair 连线（离线可用），其余切换到 pydeck 地图。
_BASEMAP_STYLES = {
    "无底图": None,
    "浅色底图": "light_nolabels",
    "道路底图": "road",
}

_TRAJECTORY_SEGMENT_COLORS = {
    "observed": [0, 102, 204, 220],
    "predicted": [255, 127, 14, 230],
    "actual_future": [44, 160, 44, 230],
}


def format_metric_value(value: str | int | float | bool | None, unit: str | None) -> str:
    """Format a scalar without changing its deterministic value."""
    if value is None:
        rendered = "—"
    elif isinstance(value, bool):
        rendered = "是" if value else "否"
    elif isinstance(value, int):
        rendered = f"{value:,}"
    elif isinstance(value, float):
        rendered = f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        rendered = value
    return f"{rendered} {unit}" if unit else rendered


def _render_metrics(block: MetricsBlock) -> None:
    st.markdown(f"##### {block.title}")
    for start in range(0, len(block.items), 4):
        items = block.items[start : start + 4]
        columns = st.columns(len(items))
        for column, item in zip(columns, items, strict=True):
            column.metric(item.label, format_metric_value(item.value, item.unit))


def _render_table(block: TableBlock) -> None:
    st.markdown(f"##### {block.title}")
    if not block.rows:
        st.info("当前查询没有可展示的记录。")
        return

    labels = {
        column.key: (f"{column.label}（{column.unit}）" if column.unit else column.label)
        for column in block.columns
    }
    display_rows = [
        {labels[column.key]: row.get(column.key) for column in block.columns} for row in block.rows
    ]
    st.dataframe(display_rows, hide_index=True, use_container_width=True)


def _render_timeseries(block: TimeSeriesBlock) -> None:
    st.markdown(f"##### {block.title}")
    if not block.points:
        st.info("当前查询没有可展示的时序点。")
        return

    x_label = f"{block.x_label}（{block.x_unit}）" if block.x_unit else block.x_label
    labels = {
        item.key: f"{item.label}（{item.unit}）" if item.unit else item.label
        for item in block.series
    }
    display_rows = [
        {
            x_label: point.get(block.x_key),
            **{label: point.get(key) for key, label in labels.items()},
        }
        for point in block.points
    ]
    st.line_chart(
        display_rows,
        x=x_label,
        y=list(labels.values()),
        use_container_width=True,
    )


def _render_trajectory_2d(
    block: Trajectory2dBlock,
    widget_prefix: str | None = None,
) -> None:
    st.markdown(f"##### {block.title}")
    if not block.points:
        st.info("当前查询没有可展示的轨迹点。")
        return

    points = block.points
    if block.snapshot_key and block.snapshot_label:
        snapshot_options = list(
            dict.fromkeys(
                str(point[block.snapshot_key])
                for point in block.points
                if point.get(block.snapshot_key)
            )
        )
        if snapshot_options:
            snapshot_key = (
                f"trajectory_2d_{widget_prefix}_snapshot"
                if widget_prefix
                else _next_trajectory_widget_key()
            )
            selected_snapshot = st.selectbox(
                block.snapshot_label,
                options=snapshot_options,
                key=snapshot_key,
            )
            points = [
                point
                for point in block.points
                if point.get(block.snapshot_key) == selected_snapshot
            ]

    x_label = f"{block.x_label}（{block.x_unit}）" if block.x_unit else block.x_label
    y_label = f"{block.y_label}（{block.y_unit}）" if block.y_unit else block.y_label
    series_labels = {
        item.key: f"{item.label}（{item.unit}）" if item.unit else item.label
        for item in block.series
    }
    path_label = block.path_label if block.path_key else None
    segment_label = block.segment_label if block.segment_key else None
    records: list[dict[str, Any]] = []
    for point in points:
        record: dict[str, Any] = {
            x_label: point.get(block.x_key),
            y_label: point.get(block.y_key),
        }
        if block.time_key:
            record["时间步（s）"] = point.get(block.time_key)
        if block.path_key and path_label:
            record[path_label] = point.get(block.path_key)
        if block.segment_key and segment_label:
            record[segment_label] = point.get(block.segment_key)
        for key, label in series_labels.items():
            record[label] = point.get(key)
        records.append(record)

    frame = pd.DataFrame(records)
    tooltip_keys = [label for label in frame.columns if label not in {x_label}]
    encoding: dict[str, Any] = {
        "x": alt.X(x_label, type="quantitative", scale=alt.Scale(zero=False)),
        "y": alt.Y(y_label, type="quantitative", scale=alt.Scale(zero=False)),
        "tooltip": tooltip_keys,
    }
    if path_label:
        encoding["detail"] = alt.Detail(path_label, type="nominal")
    if "时间步（s）" in frame.columns:
        encoding["order"] = alt.Order("时间步（s）", type="quantitative")
    if segment_label:
        encoding["color"] = alt.Color(segment_label, type="nominal")
    chart = alt.Chart(frame).mark_line(point=True).encode(**encoding)
    color_label = next(
        (
            label
            for label in series_labels.values()
            if label in frame.columns and frame[label].notna().any()
        ),
        None,
    )
    if color_label and not segment_label:
        chart = chart.encode(
            color=alt.Color(color_label, type="quantitative", legend=alt.Legend(orient="top"))
        )
    chart = chart.interactive()  # 滚轮缩放与拖拽平移

    if widget_prefix:
        enlarge_key = f"trajectory_2d_{widget_prefix}_enlarge"
        basemap_key = f"trajectory_2d_{widget_prefix}_basemap"
    else:
        # 无位置信息的调用方（如单测）回退到计数器。
        enlarge_key = _next_trajectory_widget_key()
        basemap_key = _next_trajectory_widget_key()
    enlarge = st.toggle("放大查看", key=enlarge_key)
    basemap_label = st.selectbox(
        "背景地图",
        options=list(_BASEMAP_STYLES),
        index=list(_BASEMAP_STYLES).index("道路底图"),
        key=basemap_key,
    )
    map_style = _BASEMAP_STYLES[basemap_label]
    if map_style is None:
        chart = chart.properties(height=640 if enlarge else 360)
        st.altair_chart(chart, use_container_width=True)
        return

    _render_trajectory_on_map(
        frame,
        x_label=x_label,
        y_label=y_label,
        map_style=map_style,
        enlarge=enlarge,
        path_label=path_label,
        segment_label=segment_label,
    )


def _auto_map_zoom(span_lat: float, span_lon: float) -> int:
    """按轨迹跨度估算一个可用的初始地图缩放级别。"""
    span = max(span_lat, span_lon, 0.0001)
    zoom = 14 - int(math.ceil(math.log2(span / 0.01)))
    return max(1, min(zoom, 14))


def _render_trajectory_on_map(
    frame: pd.DataFrame,
    *,
    x_label: str,
    y_label: str,
    map_style: str,
    enlarge: bool,
    path_label: str | None = None,
    segment_label: str | None = None,
) -> None:
    """用 pydeck 在真实地图底图上绘制轨迹线。"""
    if pdk is None:
        st.warning("地图底图需要 pydeck 依赖，请安装 pydeck 后再试。")
        return

    valid = frame.dropna(subset=[x_label, y_label]).copy()
    if len(valid) < 2:
        st.info("轨迹点缺少有效坐标，无法在地图上展示。")
        return

    center_lat = float(valid[y_label].mean())
    center_lon = float(valid[x_label].mean())
    span_lat = float(valid[y_label].max() - valid[y_label].min())
    span_lon = float(valid[x_label].max() - valid[x_label].min())

    tooltip_parts = [f"{{{x_label}}}", f"{{{y_label}}}"]
    if "时间步（s）" in frame.columns:
        tooltip_parts.insert(0, "{时间步（s）}")

    # 整条轨迹作为一个 path 画线（LineLayer 每条记录只画一段，
    # 起点=终点会退化为零长度线段而不可见）。
    group_columns = [label for label in (path_label, segment_label) if label]
    if group_columns:
        grouped = valid.groupby(group_columns, sort=False, dropna=False)
    else:
        grouped = [((), valid)]
    paths = []
    for group, rows in grouped:
        if "时间步（s）" in rows.columns:
            rows = rows.sort_values("时间步（s）")
        coords = [
            [float(lon), float(lat)] for lon, lat in zip(rows[x_label], rows[y_label], strict=True)
        ]
        if len(coords) < 2:
            continue
        values = group if isinstance(group, tuple) else (group,)
        metadata = dict(zip(group_columns, values, strict=True))
        segment = str(metadata.get(segment_label, "")) if segment_label else ""
        paths.append(
            {
                "path": coords,
                "color": _TRAJECTORY_SEGMENT_COLORS.get(segment, [100, 100, 100, 210]),
                **metadata,
            }
        )
    path_layer = pdk.Layer(
        "PathLayer",
        data=pd.DataFrame(paths),
        get_path="path",
        get_color="color",
        get_width=10,
        width_min_pixels=3,
        pickable=True,
    )
    # 轨迹点单独一层，明显可见且支持悬停 tooltip。
    if segment_label:
        valid["_segment_color"] = valid[segment_label].map(
            lambda value: _TRAJECTORY_SEGMENT_COLORS.get(str(value), [100, 100, 100, 200])
        )
    else:
        valid["_segment_color"] = [[0, 102, 204, 200]] * len(valid)
    point_layer = pdk.Layer(
        "ScatterplotLayer",
        data=valid,
        get_position=[x_label, y_label],
        get_radius=80,
        radius_min_pixels=2.5,
        get_fill_color="_segment_color",
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[path_layer, point_layer],
        initial_view_state=pdk.ViewState(
            latitude=center_lat,
            longitude=center_lon,
            zoom=_auto_map_zoom(span_lat, span_lon),
            pitch=0,
        ),
        map_style=map_style,
        tooltip={"html": "<br>".join(tooltip_parts)},
    )
    st.pydeck_chart(deck, height=640 if enlarge else 360, use_container_width=True)
    if segment_label:
        st.caption("蓝色：观测段 · 橙色：预测段 · 绿色：真实未来段")
    st.caption("底图瓦片需联网加载；无法联网时轨迹线仍会显示。")


def _render_classification(block: ClassificationBlock) -> None:
    st.markdown(f"##### {block.title}")
    if not block.items:
        st.info("当前查询没有可展示的分类结果。")
        return
    rows = [
        {"类别": item.label, "数量": item.count, "置信度": item.confidence} for item in block.items
    ]
    frame = pd.DataFrame(rows)
    st.dataframe(frame, hide_index=True, use_container_width=True)
    counts = [item.count for item in block.items]
    if any(count > 0 for count in counts):
        st.bar_chart(frame, x="类别", y="数量")


def _render_prediction(block: PredictionBlock) -> None:
    st.markdown(f"##### {block.title}")
    st.caption(f"来源：{block.source}")
    if not block.points:
        st.info("当前查询没有可展示的预测点。")
        return
    target_label = (
        f"{block.target_label}（{block.target_unit}）" if block.target_unit else block.target_label
    )
    rows = [
        {
            "时间步（s）": point.time_step_s,
            target_label: point.value,
            "下界": point.lower_bound,
            "上界": point.upper_bound,
        }
        for point in block.points
    ]
    st.line_chart(
        rows,
        x="时间步（s）",
        y=[target_label, "下界", "上界"],
        use_container_width=True,
    )


def _render_action_link(block: ActionLinkBlock) -> None:
    st.link_button(block.label, block.url, type="primary", use_container_width=True)


def render_analysis_result(
    result: AnalysisResult,
    widget_prefix: str | None = None,
) -> None:
    """Render supported blocks without branching on dataset-specific fields.

    widget_prefix 用于让控件 key 跨 rerun 稳定：由调用方按消息在对话历史中的
    位置生成（例如 m3_tool_0），保证同一轮内唯一且刷新后不变。
    """
    st.markdown(f"#### {result.title}")
    dataset_label = {
        "scat": "SCAT",
        "uav": "UAV",
        "cross_dataset": "跨数据集",
    }[result.dataset]
    st.caption(
        f"数据集：{dataset_label} · 操作：{result.operation} · 协议：{result.schema_version}"
    )
    if result.start_time:
        st.caption(f"时序开始时间（UTC）：{result.start_time}")
    for index, block in enumerate(result.blocks):
        if isinstance(block, MetricsBlock):
            _render_metrics(block)
        elif isinstance(block, TableBlock):
            _render_table(block)
        elif isinstance(block, TimeSeriesBlock):
            _render_timeseries(block)
        elif isinstance(block, Trajectory2dBlock):
            prefix = f"{widget_prefix}_{index}" if widget_prefix else None
            _render_trajectory_2d(block, prefix)
        elif isinstance(block, ClassificationBlock):
            _render_classification(block)
        elif isinstance(block, PredictionBlock):
            _render_prediction(block)
        elif isinstance(block, ActionLinkBlock):
            _render_action_link(block)


__all__ = ["format_metric_value", "render_analysis_result"]
