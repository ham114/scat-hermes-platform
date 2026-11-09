"""Tests for deterministic aviation AnalysisResult builders."""

from agents.analysis_results import (
    build_scat_batch_analysis,
    build_scat_flight_overview_analysis,
    build_scat_flight_search_analysis,
    build_scat_track_analysis,
    build_uav_flight_overview_analysis,
    build_uav_flight_search_analysis,
    build_uav_model_analysis,
    build_uav_samples_analysis,
)
from schema.analysis import (
    MetricsBlock,
    TableBlock,
    TimeSeriesBlock,
    Trajectory2dBlock,
)


def test_scat_batch_builder_creates_metrics_and_distribution_tables():
    result = build_scat_batch_analysis(
        {
            "status": "ok",
            "query": {"batch": "batch-a"},
            "data": {
                "statistics": {
                    "batch": "batch-a",
                    "flight_count": 10,
                    "unique_callsign_count": 8,
                    "total_track_points": 1200,
                    "flights_with_prediction": 9,
                },
                "top_aircraft_types": [{"aircraft_type": "A320", "flight_count": 6}],
                "top_routes": [{"adep": "ZBAA", "ades": "ZSSS", "flight_count": 4}],
            },
        }
    )

    assert result is not None
    assert result.dataset == "scat"
    assert result.context == {"batch": "batch-a"}
    assert isinstance(result.blocks[0], MetricsBlock)
    assert [block.title for block in result.blocks[1:] if isinstance(block, TableBlock)] == [
        "主要机型",
        "主要航线",
    ]


def test_uav_model_builder_calculates_total_and_keeps_bounded_rows():
    result = build_uav_model_analysis(
        {
            "status": "ok",
            "data": {
                "items": [
                    {
                        "model": "0001",
                        "flight_count": 14,
                        "earliest_takeoff_utc": "2022-07-23T04:40:28Z",
                        "latest_takeoff_utc": "2023-04-26T01:37:51Z",
                    },
                    {
                        "model": "0007",
                        "flight_count": 25,
                        "earliest_takeoff_utc": "2022-11-11T05:17:03Z",
                        "latest_takeoff_utc": "2024-07-09T22:54:58Z",
                    },
                ]
            },
        }
    )

    assert result is not None
    metrics = result.blocks[0]
    table = result.blocks[1]
    assert isinstance(metrics, MetricsBlock)
    assert metrics.items[0].value == 39
    assert isinstance(table, TableBlock)
    assert len(table.rows) == 2


def test_builders_do_not_create_visuals_for_failed_queries():
    failed = {"status": "database_error", "data": None}

    assert build_scat_batch_analysis(failed) is None
    assert build_scat_flight_overview_analysis(failed) is None
    assert build_scat_flight_search_analysis(failed) is None
    assert build_scat_track_analysis(failed) is None
    assert build_uav_flight_search_analysis(failed) is None
    assert build_uav_flight_overview_analysis(failed) is None
    assert build_uav_model_analysis(failed) is None
    assert build_uav_samples_analysis(failed) is None


def test_scat_search_builder_flattens_rows_and_preserves_pagination():
    result = build_scat_flight_search_analysis(
        {
            "status": "ok",
            "query": {"batch": "week1", "aircraft_type": "B738", "skip": 20},
            "data": {
                "count": 1,
                "total": 23,
                "has_more": True,
                "items": [
                    {
                        "source": {"external_id": 1394},
                        "identity": {
                            "callsign": "IBK8WB",
                            "aircraft_type": "B738",
                            "departure_airport": "EGKK",
                            "destination_airport": "ESSA",
                            "iobt_utc": "2022-06-01T08:00:00Z",
                        },
                        "control_centre_transfer_count": 2,
                    }
                ],
            },
        }
    )

    assert result is not None
    assert result.context["skip"] == 20
    metrics, table = result.blocks
    assert isinstance(metrics, MetricsBlock)
    assert [item.value for item in metrics.items] == [23, 1, True]
    assert isinstance(table, TableBlock)
    assert table.rows[0]["callsign"] == "IBK8WB"
    assert table.rows[0]["departure_airport"] == "EGKK"


def test_search_builders_keep_empty_tables_and_cap_rows_at_100():
    empty = build_uav_flight_search_analysis(
        {
            "status": "ok",
            "query": {"limit": 20, "offset": 0},
            "data": {
                "pagination": {
                    "offset": 0,
                    "limit": 20,
                    "returned": 0,
                    "total": 0,
                    "has_more": False,
                },
                "items": [],
            },
        }
    )
    assert empty is not None
    empty_table = empty.blocks[1]
    assert isinstance(empty_table, TableBlock)
    assert empty_table.rows == []

    many_items = [
        {
            "external_id": f"flight-{index}",
            "model": "0001",
            "sample_count": 4506,
        }
        for index in range(105)
    ]
    bounded = build_uav_flight_search_analysis(
        {
            "status": "ok",
            "data": {
                "pagination": {
                    "returned": 105,
                    "total": 105,
                    "has_more": False,
                },
                "items": many_items,
            },
        }
    )
    assert bounded is not None
    bounded_table = bounded.blocks[1]
    assert isinstance(bounded_table, TableBlock)
    assert len(bounded_table.rows) == 100
    assert bounded_table.rows[-1]["external_id"] == "flight-99"


def test_uav_flight_overview_builder_uses_channel_catalog_units():
    result = build_uav_flight_overview_analysis(
        {
            "status": "ok",
            "data": {
                "external_id": "flight-1",
                "model": "0001",
                "mission": "DTC",
                "takeoff_at": "2023-03-30T04:27:40Z",
                "duration_s": 4505.0,
                "sample_count": 4506,
                "sample_rate_hz": 1.0,
                "flight_kind": "normal",
                "summary": {
                    "altitude_m": {"min": 9.74, "max": 793.2},
                    "ground_speed_mps": {"min": 0.0, "max": 118.1},
                },
                "channel_stats": [
                    {
                        "key": "cmp_comb_alt",
                        "min": 9.74,
                        "max": 793.2,
                        "avg": 412.5,
                        "missing": 0,
                    }
                ],
            },
        }
    )

    assert result is not None
    metrics, table = result.blocks
    assert isinstance(metrics, MetricsBlock)
    assert next(item for item in metrics.items if item.key == "duration_s").unit == "s"
    assert next(item for item in metrics.items if item.key == "altitude_max").unit == "m"
    assert isinstance(table, TableBlock)
    assert table.rows[0] == {
        "key": "cmp_comb_alt",
        "label": "组合高度",
        "group": "position",
        "unit": "m",
        "min": 9.74,
        "max": 793.2,
        "avg": 412.5,
        "missing": 0,
    }


def test_scat_flight_overview_builder_creates_metrics_and_vertical_modes():
    result = build_scat_flight_overview_analysis(
        {
            "status": "ok",
            "query": {"batch": "week1", "external_id": "1394"},
            "data": {
                "flight": {
                    "source": {"external_id": 1394},
                    "identity": {
                        "callsign": "IBK8WB",
                        "aircraft_type": "B738",
                        "departure_airport": "EGKK",
                        "destination_airport": "ESSA",
                        "iobt_utc": "2016-10-20T18:45:00Z",
                    },
                },
                "trajectory": {
                    "point_count": 100,
                    "duration_minutes": 30.0,
                    "altitude": {"average_metres": 5000.0, "maximum_metres": 9000.0},
                    "ground_speed": {"average_mps": 180.0, "maximum_mps": 220.0},
                    "vertical_mode_counts": {
                        "level": 40,
                        "climb": 30,
                        "descent": 30,
                        "uncertain": 0,
                    },
                    "vertical_mode_ratios": {
                        "level": 0.4,
                        "climb": 0.3,
                        "descent": 0.3,
                        "uncertain": 0.0,
                    },
                    "data_quality": {"is_model_input_candidate": True},
                },
                "scat_prediction": {"snapshot_count": 8},
            },
        }
    )

    assert result is not None
    metrics, table = result.blocks
    assert isinstance(metrics, MetricsBlock)
    assert next(item for item in metrics.items if item.key == "route").value == "EGKK → ESSA"
    assert isinstance(table, TableBlock)
    assert table.rows[0] == {"mode": "level", "label": "平飞", "count": 40, "ratio": 0.4}


def test_scat_track_builder_groups_units_and_caps_display_points():
    points = [
        {
            "sequence": index,
            "timestamp": (
                f"2016-10-20T20:{index // 60:02d}:{index % 60:02d}Z"
            ),
            "position": {
                "lat_deg": 51.0 + index / 1000,
                "lon_deg": 0.1,
                "altitude_m": 1000.0 + index,
            },
            "motion": {
                "ground_speed_mps": 180.0,
                "vertical_speed_mps": 2.0,
                "track_angle_deg": 90.0,
            },
        }
        for index in range(205)
    ]
    result = build_scat_track_analysis(
        {
            "status": "ok",
            "query": {"external_id": "1394", "every_nth": 2},
            "data": {
                "flight": {"source": {"external_id": 1394}},
                "pagination": {
                    "returned_points": 205,
                    "total_points": 1000,
                    "has_more": True,
                },
                "points": points,
            },
        }
    )

    assert result is not None
    assert result.schema_version == "1.3"
    assert result.start_time == "2016-10-20T20:00:00Z"
    series_blocks = [block for block in result.blocks if isinstance(block, TimeSeriesBlock)]
    assert {block.series[0].unit for block in series_blocks} >= {"deg", "m", "m/s"}
    speed_block = next(block for block in series_blocks if block.series[0].unit == "m/s")
    assert [series.key for series in speed_block.series] == [
        "ground_speed",
        "vertical_speed",
    ]
    assert len(speed_block.points) == 200
    assert speed_block.x_key == "time_step_s"
    assert speed_block.x_type == "number"
    assert speed_block.x_unit == "s"
    assert speed_block.points[1]["time_step_s"] == 1.0
    assert "timestamp" not in speed_block.points[0]

    trajectory = next(
        block for block in result.blocks if isinstance(block, Trajectory2dBlock)
    )
    assert trajectory.x_key == "longitude"
    assert trajectory.y_key == "latitude"
    assert trajectory.time_key == "time_step_s"
    assert trajectory.series[0].key == "altitude"
    assert len(trajectory.points) == 200
    assert trajectory.points[0]["longitude"] == 0.1
    assert trajectory.points[1]["latitude"] == 51.001


def test_uav_samples_builder_uses_catalog_labels_and_separates_units():
    result = build_uav_samples_analysis(
        {
            "status": "ok",
            "query": {
                "every_nth": 5,
                "channels": ["cmp_comb_alt", "cmp_ground_speed", "cmp_pitch"],
            },
            "data": {
                "external_id": "flight-1",
                "start_time": "2023-03-30T04:27:40Z",
                "limit": 100,
                "every_nth": 5,
                "returned": 2,
                "has_more": False,
                "samples": [
                    {
                        "seq": 0,
                        "t_s": 0.0,
                        "cmp_comb_alt": 10.0,
                        "cmp_ground_speed": 3.0,
                        "cmp_pitch": 1.0,
                    },
                    {
                        "seq": 5,
                        "t_s": 5.0,
                        "cmp_comb_alt": 12.0,
                        "cmp_ground_speed": 4.0,
                        "cmp_pitch": 2.0,
                    },
                ],
            },
        }
    )

    assert result is not None
    assert result.schema_version == "1.2"
    assert result.start_time == "2023-03-30T04:27:40Z"
    series_blocks = [block for block in result.blocks if isinstance(block, TimeSeriesBlock)]
    assert len(series_blocks) == 3
    altitude = next(block for block in series_blocks if block.series[0].unit == "m")
    assert altitude.series[0].label == "组合高度"
    assert altitude.x_key == "time_step_s"
    assert altitude.points[0]["time_step_s"] == 0.0
    assert altitude.points[1]["time_step_s"] == 5.0
    assert "t_s" not in altitude.points[0]
    assert altitude.points[1]["cmp_comb_alt"] == 12.0


def test_uav_samples_builder_adds_trajectory_2d_when_position_channels_present():
    result = build_uav_samples_analysis(
        {
            "status": "ok",
            "query": {"every_nth": 1, "channels": ["cmp_comb_lon", "cmp_comb_lat", "cmp_comb_alt"]},
            "data": {
                "external_id": "flight-2",
                "start_time": "2023-03-30T04:27:40Z",
                "limit": 100,
                "every_nth": 1,
                "returned": 3,
                "has_more": False,
                "samples": [
                    {"seq": 0, "t_s": 0.0, "cmp_comb_lon": 116.0, "cmp_comb_lat": 28.0, "cmp_comb_alt": 10.0},
                    {"seq": 1, "t_s": 1.0, "cmp_comb_lon": 116.01, "cmp_comb_lat": 28.01, "cmp_comb_alt": 11.0},
                    {"seq": 2, "t_s": 2.0, "cmp_comb_lon": 116.02, "cmp_comb_lat": 28.02, "cmp_comb_alt": 12.0},
                ],
            },
        }
    )

    assert result is not None
    assert result.schema_version == "1.3"
    trajectory = next(
        block for block in result.blocks if isinstance(block, Trajectory2dBlock)
    )
    assert trajectory.title == "UAV 二维轨迹"
    assert trajectory.x_key == "cmp_comb_lon"
    assert trajectory.y_key == "cmp_comb_lat"
    assert trajectory.time_key == "time_step_s"
    assert trajectory.series[0].key == "cmp_comb_alt"
    assert trajectory.points[2] == {
        "cmp_comb_lon": 116.02,
        "cmp_comb_lat": 28.02,
        "time_step_s": 2.0,
        "cmp_comb_alt": 12.0,
    }
