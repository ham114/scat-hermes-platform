"""Tests for the versioned structured analysis protocol."""

import pytest
from pydantic import ValidationError

from schema.analysis import (
    ActionLinkBlock,
    AnalysisResult,
    ClassificationBlock,
    ClassificationItem,
    MetricsBlock,
    PredictionBlock,
    PredictionPoint,
    TableBlock,
    TimeSeriesBlock,
    TimeSeriesSeries,
    Trajectory2dBlock,
    Trajectory2dSeries,
    extract_analysis_result,
)


def test_extract_analysis_result_from_serialized_tool_payload():
    payload = """{
      "status": "ok",
      "analysis_result": {
        "schema_version": "1.0",
        "dataset": "uav",
        "operation": "list_uav_models",
        "title": "UAV 统计",
        "context": {},
        "blocks": [
          {
            "type": "metrics",
            "title": "规模",
            "items": [{"key": "count", "label": "架次数", "value": 42, "unit": "架次"}]
          },
          {
            "type": "table",
            "title": "机型",
            "columns": [{"key": "model", "label": "机型", "unit": null}],
            "rows": [{"model": "0001"}]
          }
        ]
      }
    }"""

    result = extract_analysis_result(payload)

    assert isinstance(result, AnalysisResult)
    assert isinstance(result.blocks[0], MetricsBlock)
    assert isinstance(result.blocks[1], TableBlock)
    assert result.blocks[0].items[0].value == 42


def test_extract_analysis_result_rejects_unknown_or_malformed_protocol():
    assert extract_analysis_result("not json") is None
    assert extract_analysis_result({"status": "ok"}) is None
    assert (
        extract_analysis_result(
            {
                "analysis_result": {
                    "schema_version": "2.0",
                    "dataset": "uav",
                    "operation": "x",
                    "title": "x",
                    "blocks": [],
                }
            }
        )
        is None
    )


def test_timeseries_v12_requires_one_start_time_and_seconds_axis():
    block = TimeSeriesBlock(
        title="高度",
        x_key="t_s",
        x_label="相对时间",
        x_type="number",
        x_unit="s",
        series=[TimeSeriesSeries(key="altitude", label="高度", unit="m")],
        points=[{"t_s": 0.0, "altitude": 10.0}],
    )
    result = AnalysisResult(
        schema_version="1.2",
        dataset="uav",
        operation="get_uav_samples",
        title="时序",
        start_time="2023-03-30T04:27:40+00:00",
        blocks=[block],
    )

    assert result.schema_version == "1.2"
    assert result.start_time == "2023-03-30T04:27:40Z"
    assert result.blocks[0].points[0]["altitude"] == 10.0

    with pytest.raises(ValidationError, match="schema_version 1.1"):
        AnalysisResult(
            dataset="uav",
            operation="get_uav_samples",
            title="时序",
            blocks=[block],
        )

    with pytest.raises(ValidationError, match="require start_time"):
        AnalysisResult(
            schema_version="1.2",
            dataset="uav",
            operation="get_uav_samples",
            title="时序",
            blocks=[block],
        )

    with pytest.raises(ValidationError, match="numeric seconds"):
        AnalysisResult(
            schema_version="1.2",
            dataset="scat",
            operation="get_scat_track_points",
            title="时序",
            start_time="2016-10-20T20:00:00Z",
            blocks=[
                TimeSeriesBlock(
                    title="高度",
                    x_key="timestamp",
                    x_label="UTC 时间",
                    x_type="datetime",
                    series=[TimeSeriesSeries(key="altitude", label="高度")],
                    points=[
                        {
                            "timestamp": "2016-10-20T20:00:00Z",
                            "altitude": 10.0,
                        }
                    ],
                )
            ],
        )

    with pytest.raises(ValidationError, match="numeric or null"):
        TimeSeriesBlock(
            title="非法时序",
            x_key="t_s",
            x_label="相对时间",
            x_type="number",
            series=[TimeSeriesSeries(key="altitude", label="高度")],
            points=[{"t_s": 0.0, "altitude": "not-a-number"}],
        )


def _trajectory_block() -> Trajectory2dBlock:
    return Trajectory2dBlock(
        title="二维轨迹",
        x_key="longitude",
        x_label="经度",
        x_unit="deg",
        y_key="latitude",
        y_label="纬度",
        y_unit="deg",
        time_key="time_step_s",
        series=[Trajectory2dSeries(key="altitude", label="高度", unit="m")],
        points=[
            {"longitude": 116.0, "latitude": 28.0, "time_step_s": 0.0, "altitude": 10.0},
            {"longitude": 116.1, "latitude": 28.1, "time_step_s": 1.0},
        ],
    )


def test_trajectory_2d_block_rejects_non_numeric_axes():
    with pytest.raises(ValidationError, match="numeric x/y"):
        Trajectory2dBlock(
            title="坏轨迹",
            x_key="longitude",
            x_label="经度",
            y_key="latitude",
            y_label="纬度",
            points=[{"longitude": "east", "latitude": 28.0}],
        )


def test_trajectory_2d_block_rejects_undeclared_point_keys():
    with pytest.raises(ValidationError, match="undeclared key"):
        Trajectory2dBlock(
            title="坏轨迹",
            x_key="longitude",
            x_label="经度",
            y_key="latitude",
            y_label="纬度",
            points=[{"longitude": 116.0, "latitude": 28.0, "bogus": 1}],
        )


def test_new_blocks_require_schema_version_1_3():
    with pytest.raises(ValidationError, match="require schema_version 1.3"):
        AnalysisResult(
            schema_version="1.2",
            dataset="scat",
            operation="get_scat_track_points",
            title="旧版本",
            blocks=[_trajectory_block()],
        )


def test_new_blocks_accept_schema_version_1_3():
    result = AnalysisResult(
        schema_version="1.3",
        dataset="cross_dataset",
        operation="mixed",
        title="新版",
        blocks=[
            _trajectory_block(),
            ClassificationBlock(
                title="分类",
                class_labels=["normal", "anomaly"],
                items=[ClassificationItem(label="normal", count=39)],
            ),
            PredictionBlock(
                title="预测",
                source="registered-model-v1",
                target_label="高度",
                target_unit="m",
                points=[
                    PredictionPoint(
                        time_step_s=1.0,
                        value=100.0,
                        lower_bound=90.0,
                        upper_bound=110.0,
                    )
                ],
            ),
            ActionLinkBlock(
                label="打开 3D 视图",
                url="/visualizations/artifact-1/3d",
                kind="visualization_3d",
                artifact_id="artifact-1",
            ),
        ],
    )
    assert result.schema_version == "1.3"
    assert len(result.blocks) == 4


def test_trajectory_1_4_accepts_path_and_segment_grouping():
    result = AnalysisResult(
        schema_version="1.4",
        dataset="scat",
        operation="batch_predict",
        title="批量预测",
        blocks=[
            Trajectory2dBlock(
                title="轨迹",
                x_key="lon",
                x_label="经度",
                y_key="lat",
                y_label="纬度",
                path_key="flight",
                path_label="航班",
                segment_key="segment",
                segment_label="分段",
                points=[
                    {"lon": 1.0, "lat": 2.0, "flight": "100001", "segment": "observed"},
                    {"lon": 1.1, "lat": 2.1, "flight": "100001", "segment": "predicted"},
                ],
            )
        ],
    )

    assert result.schema_version == "1.4"


def test_trajectory_1_5_accepts_snapshot_grouping_and_rejects_it_on_1_4():
    block = Trajectory2dBlock(
        title="多快照轨迹",
        x_key="lon",
        x_label="经度",
        y_key="lat",
        y_label="纬度",
        time_key="time_s",
        path_key="method",
        path_label="方法",
        snapshot_key="snapshot",
        snapshot_label="快照",
        points=[
            {"lon": 1.0, "lat": 2.0, "time_s": 10.0, "method": "actual", "snapshot": "a"},
            {"lon": 1.1, "lat": 2.1, "time_s": 20.0, "method": "actual", "snapshot": "a"},
        ],
    )

    result = AnalysisResult(
        schema_version="1.5",
        dataset="scat",
        operation="comparison",
        title="多快照",
        blocks=[block],
    )
    assert result.blocks[0].snapshot_key == "snapshot"

    with pytest.raises(ValidationError, match="snapshot grouping requires schema_version 1.5"):
        AnalysisResult(
            schema_version="1.4",
            dataset="scat",
            operation="comparison",
            title="错误版本",
            blocks=[block],
        )


def test_classification_item_label_must_be_declared():
    with pytest.raises(ValidationError, match="class_labels"):
        ClassificationBlock(
            title="分类",
            class_labels=["normal"],
            items=[ClassificationItem(label="anomaly")],
        )


def test_prediction_point_rejects_inverted_bounds():
    with pytest.raises(ValidationError, match="lower_bound"):
        PredictionPoint(time_step_s=0.0, value=10.0, lower_bound=12.0)
    with pytest.raises(ValidationError, match="upper_bound"):
        PredictionPoint(time_step_s=0.0, value=10.0, upper_bound=8.0)


def test_action_link_url_must_be_http_or_relative():
    with pytest.raises(ValidationError, match="http"):
        ActionLinkBlock(label="坏链接", url="javascript:alert(1)")
    link = ActionLinkBlock(label="跳转", url="/visualizations/abc/3d")
    assert link.kind == "external"


def test_trajectory_third_series_requires_schema_version_1_6():
    block = Trajectory2dBlock(
        title="带误差轨迹",
        x_key="lon",
        x_label="经度",
        y_key="lat",
        y_label="纬度",
        series=[
            Trajectory2dSeries(key="altitude", label="高度", unit="m"),
            Trajectory2dSeries(key="speed", label="速度", unit="m/s"),
            Trajectory2dSeries(key="horizontal_error", label="水平误差", unit="m"),
        ],
        points=[
            {
                "lon": 1.0,
                "lat": 2.0,
                "altitude": 3.0,
                "speed": 4.0,
                "horizontal_error": 5.0,
            }
        ],
    )
    with pytest.raises(ValidationError, match="third series requires schema_version 1.6"):
        AnalysisResult(
            schema_version="1.5",
            dataset="scat",
            operation="comparison",
            title="错误版本",
            blocks=[block],
        )
    assert (
        AnalysisResult(
            schema_version="1.6",
            dataset="scat",
            operation="comparison",
            title="正确版本",
            blocks=[block],
        ).schema_version
        == "1.6"
    )
