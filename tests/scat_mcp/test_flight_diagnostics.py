from datetime import UTC, datetime, timedelta

import pytest

from application.flight_diagnostics import (
    batch_comparison,
    prediction_errors,
    track_quality,
    uav_channels,
)
from application.readonly_display import receipt_display


def block(result, title):
    return next(
        item for item in result["analysis_result"]["blocks"] if item["title"].startswith(title)
    )


def test_uav_diagnostic_pairs_only_unique_equal_time_rows_and_excludes_constant_channels():
    channels = ["cmp_comb_alt", "cmp_comb_vu"]
    values = [(i, 100 + i, 2) for i in range(4)] + [(4, 999, 500), (4, 0, -500), (None, 0, 0)]
    result = uav_channels(
        {
            "status": "ok",
            "data": {
                "samples": [dict(zip(("t_s", *channels), row, strict=True)) for row in values],
                "start_time": "2024-01-01T00:00:00Z",
                "start": 10,
                "next_start": 17,
                "limit": 7,
                "has_more": True,
            },
        },
        channels,
    )
    assert result["data"]["aligned_points"] == 4
    assert result["data"]["duplicate_time_rows"] == 2
    assert result["data"]["invalid_time"] == 1
    assert result["data"]["next_start"] == 17
    corr = block(result, "同期 Pearson")["rows"]
    assert [row["paired_count"] for row in corr] == [4, 4, 4, 4]
    assert [row["pearson_r"] for row in corr] == [pytest.approx(1), None, None, None]
    assert len(block(result, "阶段分布五数概括（m）")["rows"]) == 2


def test_uav_diagnostic_shows_recorded_relative_time_without_a_calendar_start():
    result = uav_channels(
        {
            "status": "ok",
            "data": {
                "samples": [
                    {"t_s": 13, "cmp_comb_alt": 300, "cmp_comb_vu": -2},
                    {"t_s": 14, "cmp_comb_alt": 298, "cmp_comb_vu": -2},
                ],
                "start_time": None,
                "start": 3,
                "next_start": None,
                "limit": 2,
                "has_more": False,
            },
        },
        ["cmp_comb_alt", "cmp_comb_vu"],
    )["analysis_result"]
    assert result["schema_version"] == "1.1" and result["start_time"] is None
    assert [b["points"][0]["t_s"] for b in result["blocks"] if b["type"] == "timeseries"] == [
        13,
        13,
    ]


def test_track_diagnostic_uses_utc_storage_and_wont_flag_speed_across_gaps():
    source = {
        "status": "ok",
        "query": {"external_id": "101"},
        "data": {
            "pagination": {"start": 40, "has_more": True},
            "points": [
                {
                    "sequence": 40 + index,
                    "timestamp": time,
                    "position": {"lat_deg": latitude, "lon_deg": 110, "altitude_m": alt},
                }
                for index, (time, latitude, alt) in enumerate(
                    [
                        ("2024-01-01T00:00:00", 30, 1000),
                        ("2024-01-01T00:00:01", 31, 1100),
                        ("2024-01-01T00:00:01", 31, 1100),
                        ("2024-01-01T00:05:00", 40, 2000),
                    ]
                )
            ],
        },
    }
    result = track_quality(source, gap_s=30, max_ground_speed_mps=400, max_vertical_rate_mps=35)
    assert result["data"]["event_counts"] == {
        "疑似位置跳变": 1,
        "爬降速率超阈值": 1,
        "重复时间戳": 1,
        "采样间隔过长": 1,
    }
    assert result["analysis_result"]["start_time"] == "2024-01-01T00:00:00Z"
    event = block(result, "事件明细")["rows"][-1]
    assert (event["start_seq"], event["end_seq"], event["focus_time_s"]) == (42, 43, 300)
    assert sum(row["count"] for row in block(result, "本页正采样间隔分布")["rows"]) == 2
    receipt = {
        "operation": result["operation"],
        "result_id": "receipt",
        "payload_sha256": "a" * 64,
        "result": result,
    }
    assert receipt_display(receipt)["analysis_result"]["context"]["display_scope"] == "fetched_page"
    assert "readonly_result_id" not in result["analysis_result"]["context"]


def test_prediction_distribution_only_describes_returned_truth_matched_details():
    source = {
        "status": "ok",
        "data": {
            "matched_fix_count": 10,
            "unmatched_fix_count": 2,
            "fixes": [{"vertical_error_m": n} for n in (-4, 2, 2, None)],
        },
    }
    result = prediction_errors(source, "vertical")
    assert result["data"]["usable"] == 3 and result["data"]["matched"] == 10
    assert result["data"]["truncated"]
    assert result["data"]["p50"] == 2
    assert block(result, "垂直有符号误差经验累计分布")["rows"][-1] == {
        "error": 2,
        "cdf_percent": 100,
    }
    assert block(result, "绝对误差最大")["rows"][0]["error"] == -4
    empty = prediction_errors(source, "horizontal")
    assert empty["data"]["p95"] is None
    assert not any("直方图" in item["title"] for item in empty["analysis_result"]["blocks"])


def test_track_diagnostic_preserves_total_events_under_detail_cap():
    origin = datetime(2024, 1, 1, tzinfo=UTC)
    result = track_quality(
        {
            "status": "ok",
            "data": {
                "pagination": {"start": 0, "has_more": False},
                "points": [
                    {
                        "sequence": index,
                        "timestamp": (origin + timedelta(seconds=5 * index)).isoformat(),
                        "position": {"lat_deg": 30, "lon_deg": 110, "altitude_m": 1200},
                    }
                    for index in range(100)
                ],
            },
        },
        gap_s=1,
        max_ground_speed_mps=400,
        max_vertical_rate_mps=35,
    )
    assert result["data"]["events"] == 99
    assert result["data"]["returned_events"] == 60
    assert len(block(result, "事件明细")["rows"]) == 60


def test_batch_comparison_keeps_independent_denominators_and_missing_batch():
    result = batch_comparison(
        [
            {
                "status": "ok",
                "data": {
                    "statistics": {"flight_count": 20},
                    "data_quality": {"empty_trajectory_count": 2},
                    "top_aircraft_types": [{"aircraft_type": "A320", "flight_count": 5}],
                    "flight_volume": [{"period_start": "2024-01-01T00:00:00Z", "flight_count": 12}],
                },
            },
            {"status": "not_found", "data": None},
        ],
        ["a", "b"],
        "aircraft",
    )
    assert result["data"]["batches"][0]["empty_percent"] == 10
    assert result["data"]["batches"][0]["scheduled_count"] == 12
    assert result["data"]["batches"][1]["flights"] is None
    assert block(result, "每批前 10 机型")["rows"] == [
        {"batch": "a", "group": "A320", "flights": 5, "total": 20, "percent": 25}
    ]


def test_batch_comparison_reports_each_populated_batch_group_percentage():
    sources = [
        {
            "status": "ok",
            "data": {
                "statistics": {"flight_count": total},
                "top_departure_airports": [{"airport": "ZBAA", "flight_count": 1}],
            },
        }
        for total in (2, 10)
    ]
    result = batch_comparison(sources, ["a", "b"], "departure")
    assert [row["percent"] for row in block(result, "每批前 10 起飞机场")["rows"]] == [50, 10]
