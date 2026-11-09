"""Focused tests for the self-developed predictor catalog tool."""

from __future__ import annotations

import pytest

from agents.prediction_tools import list_prediction_models


@pytest.mark.asyncio
async def test_prediction_catalog_lists_versions_and_capabilities() -> None:
    result = await list_prediction_models.ainvoke({})

    assert result["status"] == "ok"
    items = result["data"]["items"]
    assert {item["predictor_id"] for item in items} == {
        "constant-velocity-heading",
        "kalman-cv",
    }
    assert all("unified_model_comparison" in item["capabilities"] for item in items)
    assert all(item["default_horizons_s"] == [60, 180, 300, 600, 900, 1200] for item in items)
