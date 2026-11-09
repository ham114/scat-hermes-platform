"""M6 分组表现测试：阶段解析与（阶段×时域）聚合。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scat.experiments.evaluation import SampleResult, aggregate_phase_metrics
from scat.experiments.grouping import phase_for_time

T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)

SEGMENTS = [
    {
        "start_time": "2016-10-20T12:00:00Z",
        "end_time": "2016-10-20T12:10:00Z",
        "phase": "climb",
    },
    {
        "start_time": "2016-10-20T12:10:00Z",
        "end_time": "2016-10-20T12:40:00Z",
        "phase": "high_cruise",
    },
]


def test_phase_for_time_matches_segment_and_handles_missing() -> None:
    assert phase_for_time(SEGMENTS, T0 + timedelta(minutes=5)) == "climb"
    # 端点重叠时归属先生成的段（保持确定性；与物化段级标注的连续性一致）
    assert phase_for_time(SEGMENTS, T0 + timedelta(minutes=10)) == "climb"
    assert phase_for_time(SEGMENTS, T0 + timedelta(minutes=15)) == "high_cruise"
    assert phase_for_time(SEGMENTS, T0 - timedelta(minutes=1)) is None
    assert phase_for_time([], T0) is None
    assert phase_for_time([{"start_time": "bad", "end_time": "bad", "phase": "x"}], T0) is None
    # 朴素时间按 UTC 解释
    assert phase_for_time(SEGMENTS, T0.replace(tzinfo=None) + timedelta(minutes=5)) == "climb"


def test_aggregate_phase_metrics_groups_and_counts_ungrouped() -> None:
    samples = [
        SampleResult("f1", T0, 60.0, 100.0),
        SampleResult("f2", T0, 60.0, 300.0),
        SampleResult("f1", T0, 180.0, 200.0),
        SampleResult("f3", T0, 60.0, 500.0),
    ]
    phases = {("f1", 60.0): "climb", ("f2", 60.0): "climb", ("f1", 180.0): "high_cruise"}

    metrics, ungrouped = aggregate_phase_metrics(
        samples,
        phase_of=lambda sample: phases.get((sample.external_id, sample.horizon_s)),
    )

    assert ungrouped == 1
    climb = next(row for row in metrics if row.phase == "climb")
    assert (climb.horizon_s, climb.sample_count, climb.flight_count) == (60.0, 2, 2)
    assert climb.mae_m == 200.0
    assert climb.median_m == 200.0
    assert climb.p90_m is not None and abs(climb.p90_m - 280.0) < 1e-9
    cruise = next(row for row in metrics if row.phase == "high_cruise")
    assert (cruise.horizon_s, cruise.sample_count, cruise.mae_m) == (180.0, 1, 200.0)
    assert [row.phase for row in metrics] == ["climb", "high_cruise"]
