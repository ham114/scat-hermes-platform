from datetime import UTC, datetime, timedelta

import pytest

from agents.analysis_results import build_comparison_model_analysis
from scat.prediction.alignment import haversine_metres
from scat_model_runtime import (
    ComparisonMethodResult,
    ComparisonMethodSummary,
    ComparisonMetricDistribution,
    ComparisonSampleResult,
    ComparisonTaskOutput,
    ComparisonTrajectoryPoint,
    DatasetKind,
    ModelInputReference,
)


def _output() -> ComparisonTaskOutput:
    origin = datetime(2026, 9, 15, tzinfo=UTC)
    methods = ("scat-gbar-v23", "kalman-cv", "constant-velocity-heading", "cfpl")
    distribution = ComparisonMetricDistribution(mean=120, median=100, p90=180)

    def sample(index: int) -> ComparisonSampleResult:
        sample_origin = origin + timedelta(hours=index)
        actual = (
            ComparisonTrajectoryPoint(
                timestamp=sample_origin + timedelta(seconds=10),
                latitude_deg=60.0 + index,
                longitude_deg=20.0 + index,
                altitude_m=3_000.0,
            ),
            ComparisonTrajectoryPoint(
                timestamp=sample_origin + timedelta(seconds=20),
                latitude_deg=60.1 + index,
                longitude_deg=20.2 + index,
                altitude_m=3_100.0,
            ),
        )
        return ComparisonSampleResult(
            sample_id=f"flight-{index + 1}:cutoff-1",
            input_ref=ModelInputReference(
                dataset=DatasetKind.SCAT,
                batch="batch",
                external_id=f"flight-{index + 1}",
            ),
            forecast_origin=sample_origin,
            time_offsets_seconds=(10.0, 20.0),
            actual_points=actual,
            method_results=tuple(
                ComparisonMethodResult(
                    method_id=method,
                    method_version="1",
                    predicted_points=actual,
                    ade_metres=100,
                    fde_metres=100,
                )
                for method in methods
            ),
        )

    return ComparisonTaskOutput(
        model_key="scat-comparison-v1",
        model_version="1",
        sample_set_sha256="a" * 64,
        samples=(sample(0), sample(1)),
        method_summaries=tuple(
            ComparisonMethodSummary(
                method_id=method,
                display_name=method,
                method_version="1",
                sample_count=2,
                covered_flights=2,
                ade_metres=distribution,
                fde_metres=distribution,
            )
            for method in methods
        ),
        excluded_counts={"truth_missing": 1},
    )


def test_comparison_analysis_contains_metrics_and_shared_axis_trajectories():
    result = build_comparison_model_analysis(
        _output(),
        artifact_id="comparison-artifact",
        thread_id="thread-1",
    )

    assert result.operation == "run_model_comparison"
    assert result.schema_version == "1.6"
    assert result.start_time == "2026-09-15T00:00:00Z"
    assert result.context["sample_set_sha256"] == "a" * 64
    assert result.context["visualization_sample_id"] == "flight-1:cutoff-1"
    assert result.context["visualization_snapshot_count"] == 2
    assert result.context["visualization_snapshot_total"] == 2
    assert [block.title for block in result.blocks[:-1]] == [
        "统一口径预测方法对比",
        "统一评估口径",
        "试点规模与时间",
        "样本排除统计",
        "可视化快照逐方法指标",
        "实际轨迹与多方法预测快照",
    ]
    table = result.blocks[0]
    assert table.type == "table"
    assert len(table.rows) == 4
    assert table.rows[0]["ade_median_m"] == 100
    assert table.rows[0]["coverage"] == 2 / 3
    trajectory = result.blocks[-2]
    assert trajectory.type == "trajectory_2d"
    assert trajectory.time_key == "time_step_s"
    assert trajectory.snapshot_key == "snapshot_id"
    assert {point["snapshot_id"] for point in trajectory.points} == {
        "flight-1 · 2026-09-15T00:00:00Z",
        "flight-2 · 2026-09-15T01:00:00Z",
    }
    assert {point["trajectory_id"] for point in trajectory.points} == {
        "actual",
        "scat-gbar-v23",
        "kalman-cv",
        "constant-velocity-heading",
        "cfpl",
    }
    assert {
        point["time_step_s"] for point in trajectory.points if point["trajectory_id"] == "actual"
    } == {10.0, 20.0}
    assert [(series.key, series.unit) for series in trajectory.series] == [
        ("altitude_m", "m"),
        ("ground_speed_mps", "m/s"),
        ("horizontal_error_m", "m"),
    ]
    first_actual = [
        point
        for point in trajectory.points
        if point["trajectory_id"] == "actual"
        and point["snapshot_id"] == "flight-1 · 2026-09-15T00:00:00Z"
    ]
    expected_ground_speed = haversine_metres(60.0, 20.0, 60.1, 20.2) / 10.0
    assert [point["ground_speed_mps"] for point in first_actual] == pytest.approx(
        [expected_ground_speed, expected_ground_speed]
    )
    assert all("horizontal_error_m" not in point for point in first_actual)
    predicted = [
        point
        for point in trajectory.points
        if point["trajectory_id"] == "scat-gbar-v23"
        and point["snapshot_id"] == "flight-1 · 2026-09-15T00:00:00Z"
    ]
    assert [point["horizontal_error_m"] for point in predicted] == pytest.approx([0.0, 0.0])
    snapshot_metrics = result.blocks[-3]
    assert snapshot_metrics.type == "table"
    assert len(snapshot_metrics.rows) == 8
    assert snapshot_metrics.rows[0]["worst_time_s"] == 10.0
    assert result.blocks[-1].type == "action_link"
    assert result.blocks[-1].artifact_id == "comparison-artifact"
