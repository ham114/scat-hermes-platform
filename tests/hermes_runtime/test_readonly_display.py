from copy import deepcopy

import pytest

from application.readonly_display import receipt_display
from hermes_runtime.results import ResultNotFoundError, ToolResultStore


def track_payload():
    return {
        "status": "ok",
        "query": {"batch": "SYNTH", "external_id": "100002", "start": 100, "every_nth": 2},
        "data": {
            "pagination": {"start": 100, "total_points": 500, "has_more": True, "next_start": 104},
            "points": [
                {
                    "sequence": 100 + i * 2,
                    "timestamp": f"2020-01-01T00:00:0{i}Z",
                    "position": {
                        "lat_deg": 50 + i * 0.01,
                        "lon_deg": 4 + i * 0.02,
                        "altitude_m": 1000 + i,
                    },
                    "motion": {"ground_speed_mps": 150 + i},
                }
                for i in range(2)
            ],
        },
    }


def test_projection_preserves_receipt_hash_units_scope_and_page(tmp_path):
    store = ToolResultStore(tmp_path / "results.sqlite", workspace_root=tmp_path)
    scope = "scat:" + "a" * 64
    raw = track_payload()
    ref = store.put(scope, "run_plot", "call", "get_scat_track_points", raw)
    receipt = store.get(scope, "run_plot", ref["result_id"])
    before = deepcopy(receipt)
    displayed = receipt_display(receipt)
    assert receipt == before
    assert displayed["result"] == raw
    assert displayed["payload_sha256"] == ref["payload_sha256"]
    analysis = displayed["analysis_result"]
    assert analysis["context"]["readonly_payload_sha256"] == ref["payload_sha256"]
    assert analysis["context"]["display_scope"] == "fetched_page"
    assert analysis["start_time"] == "2020-01-01T00:00:00Z"
    trajectory = next(b for b in analysis["blocks"] if b["type"] == "trajectory_2d")
    assert trajectory["points"][1]["longitude"] == 4.02
    assert trajectory["x_unit"] == "deg"
    assert len(trajectory["points"]) == 2
    assert store.get(scope, "run_plot", ref["result_id"]) == before
    with pytest.raises(ResultNotFoundError):
        store.get("scat:" + "b" * 64, "run_plot", ref["result_id"])


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "representation": "page_summary", "data": {"pagination": {}}},
        {"status": "ok", "data": {}},
        {"status": "timeout", "data": None},
    ],
)
def test_no_points_cannot_become_a_fabricated_chart(payload):
    receipt = {"operation": "get_scat_track_points", "result": payload}
    assert "analysis_result" not in receipt_display(receipt)


def test_uav_window_projection_keeps_absolute_origin_and_units():
    raw = {
        "status": "ok",
        "query": {"external_id": "SYNTH-UAV", "start": 100, "limit": 20},
        "data": {
            "external_id": "SYNTH-UAV",
            "start_time": "2020-01-01T00:01:40Z",
            "limit": 20,
            "every_nth": 2,
            "has_more": True,
            "samples": [
                {
                    "t_s": 100 + i * 2,
                    "cmp_comb_lon": 4 + i * 0.01,
                    "cmp_comb_lat": 50 + i * 0.01,
                    "cmp_comb_alt": 1000 + i,
                }
                for i in range(2)
            ],
        },
    }
    receipt = {
        "operation": "get_uav_samples",
        "result": raw,
        "result_id": "a" * 32,
        "payload_sha256": "b" * 64,
    }
    before = deepcopy(receipt)
    analysis = receipt_display(receipt)["analysis_result"]
    assert receipt == before
    assert analysis["start_time"] == "2020-01-01T00:01:40Z"
    assert analysis["context"]["display_scope"] == "fetched_page"
    trajectory = next(b for b in analysis["blocks"] if b["type"] == "trajectory_2d")
    assert trajectory["x_unit"] == trajectory["y_unit"] == "deg"
    assert trajectory["points"][1]["time_step_s"] == 2
    assert trajectory["points"][1]["cmp_comb_alt"] == 1001


@pytest.mark.parametrize("ready", [True, False])
def test_quality_readiness_is_not_accuracy_and_all_repair_counts_survive(ready):
    feature = {
        "feature": "vx",
        "missing_or_non_finite": 3,
        "out_of_range": 2,
        "spike": 1,
        "interpolated": 4,
        "extrapolated": 1,
        "unrepaired": 1,
    }
    raw = {
        "status": "ok",
        "query": {"batch": "SYNTH", "source_limit": 200},
        "data": {
            "model_ready": ready,
            "error_code": None if ready else "insufficient_points",
            "quality": {
                "source_points_received": 200,
                "repaired_values": 5,
                "unrepaired_values": 1,
                "features": [feature],
            },
        },
    }
    receipt = {
        "operation": "inspect_scat_model_data_quality",
        "result": raw,
        "result_id": "a" * 32,
        "payload_sha256": "b" * 64,
    }
    before = deepcopy(receipt)
    analysis = receipt_display(receipt)["analysis_result"]
    assert receipt == before
    assert analysis["blocks"][1]["rows"] == [feature]
    values = {i["key"]: i["value"] for i in analysis["blocks"][0]["items"]}
    assert values["model_ready"] is ready
    assert values["source_points_received"] == 200
    assert not {"accuracy", "rmse", "mae"}.intersection(values)


def test_failed_and_unsupported_receipts_never_gain_visuals():
    from application.readonly_display import DISPLAY_BUILDERS

    for operation in DISPLAY_BUILDERS:
        receipt = {"operation": operation, "result": {"status": "timeout", "data": None}}
        assert receipt_display(receipt) is receipt
    receipt = {"operation": "unregistered", "result": {"status": "ok", "data": {}}}
    assert receipt_display(receipt) is receipt
