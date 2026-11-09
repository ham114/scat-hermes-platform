from copy import deepcopy

import pytest
from pydantic import ValidationError

from application.readonly_display import receipt_display
from application.readonly_extensions import BoundedCfplSummaries, BoundedCfplWorst


@pytest.mark.parametrize("schema", [BoundedCfplSummaries, BoundedCfplWorst])
def test_registered_horizons_and_bounded_pages(schema):
    assert schema(horizon_s=180, offset=2).offset == 2
    for args in [{"horizon_s": 181}, {"offset": -1}, {"offset": 10001}, {"limit": 51}]:
        with pytest.raises(ValidationError):
            schema(**args)


@pytest.mark.parametrize(
    "operation,key",
    [
        ("get_prediction_metric_summaries", "summaries"),
        ("get_worst_prediction_cases", "samples"),
    ],
)
def test_cfpl_projection_retains_source_and_page_boundary(operation, key):
    receipt = {
        "operation": operation,
        "result_id": "result_test",
        "payload_sha256": "a" * 64,
        "result": {
            "status": "ok",
            "operation": operation,
            "query": {"batch": "SYNTH"},
            "data": {
                key: [{"horizon_s": 180, "horizontal_mae_m": 12.5, "horizontal_error_m": 13.5}],
                "pagination": {"offset": 20, "has_more": True, "next_offset": 21},
                "metric_protocol": "scat-prediction-metrics-v2",
            },
        },
    }
    before = deepcopy(receipt)
    projected = receipt_display(receipt)
    assert receipt == before
    analysis = projected["analysis_result"]
    assert analysis["context"]["display_scope"] == "fetched_page"
    assert analysis["context"]["page_next_offset"] == 21
    table = next(b for b in analysis["blocks"] if b["type"] == "table")
    assert table["rows"][0]["horizon_s"] == 180
    assert {c["unit"] for c in table["columns"]} >= {"s", "m"}


def test_stale_cfpl_values_do_not_become_a_chart():
    receipt = {
        "operation": "get_prediction_metric_summaries",
        "result": {"status": "stale_materialization", "data": None},
    }
    assert receipt_display(receipt) == receipt
