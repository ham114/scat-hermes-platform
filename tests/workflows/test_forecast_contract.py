"""Independent point-count, origin, no-leakage and missing-truth acceptance."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from application.context import ExecutionContext
from application.task_store import TaskConflict
from scat.prediction.baselines import ConstantVelocityHeadingPredictor
from workflow_runtime.contracts import WorkflowProposal, trial_manifest
from workflow_runtime.data import ScatSource, select_input
from workflow_runtime.numerics import run_trial, summarize
from workflow_runtime.report import build
from workflow_runtime.windows import verified_windows


def plan(**changes):
    return WorkflowProposal.model_validate(
        {
            "kind": "forecast",
            "batch": "unit",
            "external_ids": ["flight"],
            "models": [{"model": "constant-velocity-heading"}],
            "observation_last_n": 200,
            "horizons_s": [10, 30, 60],
            **changes,
        }
    )


class Source:
    version = "point-contract-test-v1"
    synthetic = True

    def __init__(self, count=524):
        # Irregular intervals make a points/seconds substitution observable.
        base = datetime(2020, 1, 1, tzinfo=UTC)
        self.points = [
            {
                "timestamp": base + timedelta(seconds=4 * i + i // 3),
                "lat": 30 + i * 0.0001,
                "lon": 110 + i * 0.0002,
                "altitude_m": 3000,
            }
            for i in range(count)
        ]
        self.gets, self.tails = 0, []

    async def get(self, batch, external_id):
        self.gets += 1
        return {"plots": deepcopy(self.points)}

    async def forecast_input(self, batch, external_id, last_n):
        self.tails.append(last_n)
        return {"plots": deepcopy(self.points[-last_n:])}


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_last_n": None},
        {"observation_last_n": 2},
        {"observation_last_n": 2001},
        {"observation_last_n": True},
        {"observation_last_n": 200.0},
        {"forecast_origin": "historical_cutoffs"},
        {"as_of": "2020-01-01T00:10:00Z"},
        {"models": [{"model": "constant-velocity-heading", "window_s": 120}]},
        {"kind": "comparison", "forecast_origin": "last_observation"},
        {"kind": "comparison", "forecast_origin": "as_of"},
        {"kind": "comparison", "forecast_origin": "as_of", "as_of": "2020-01-01T00:10:00"},
        {"materializations": ["prediction_metrics"]},
    ],
)
def test_contract_rejects_ambiguous_or_approximate_windows(changes):
    with pytest.raises(ValidationError):
        plan(**changes)


@pytest.mark.asyncio
async def test_exact_tail_origin_and_model_sees_all_200_not_200_seconds(monkeypatch):
    source = Source()
    p = plan()
    frozen = await select_input(ExecutionContext("owner", "tail"), p, source)
    assert source.tails == [200] and source.gets == 0
    flight = frozen["flights"][0]
    window = flight["observation_windows"][0]
    assert window["point_count"] == 200
    assert window["start_time"] == source.points[-200]["timestamp"].isoformat()
    assert window["forecast_origin"] == source.points[-1]["timestamp"].isoformat()
    assert window["span_s"] > 800
    seen = []
    original = ConstantVelocityHeadingPredictor.predict

    def predict(self, request):
        seen.append((self.window_s, request))
        return original(self, request)

    monkeypatch.setattr(ConstantVelocityHeadingPredictor, "predict", predict)
    output = run_trial(frozen, p.model_dump(mode="json"), trial_manifest(p)[0])
    assert len(seen) == 1 and len(seen[0][1].observed) == 200
    assert seen[0][0] == window["span_s"]
    assert seen[0][1].observed[-1].time == source.points[-1]["timestamp"]
    assert [t - seen[0][1].observed[-1].time for t in seen[0][1].target_times] == [
        timedelta(seconds=s) for s in p.horizons_s
    ]
    assert output["samples"] == [] and output["prediction_count"] == 3
    assert output["status"] == "succeeded"
    # Mutating the live source cannot move an approved forecast's origin.
    source.points.append(
        {**source.points[-1], "timestamp": source.points[-1]["timestamp"] + timedelta(days=1)}
    )
    assert run_trial(frozen, p.model_dump(mode="json"), trial_manifest(p)[0]) == output
    changed = deepcopy(flight)
    changed["observation_windows"][0]["point_count"] = 199
    with pytest.raises(TaskConflict):
        verified_windows(changed, p)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, 199, 200, 2000])
async def test_exact_short_and_upper_bound_inputs(count):
    p = plan(observation_last_n=count)
    frozen = await select_input(ExecutionContext("owner", "boundary"), p, Source(count))
    assert frozen["flights"][0]["observation_windows"][0]["point_count"] == count


@pytest.mark.asyncio
async def test_insufficient_duplicates_and_invalid_rows_are_not_filled_or_dropped():
    with pytest.raises(TaskConflict, match="insufficient_exact"):
        await select_input(ExecutionContext("owner", "short"), plan(), Source(199))
    for bad in ["duplicate", "invalid", "gap"]:
        source = Source(200)
        if bad == "duplicate":
            source.points[-1]["timestamp"] = source.points[-2]["timestamp"]
        elif bad == "invalid":
            source.points[-1]["lat"] = 100
        else:
            source.points[-1]["timestamp"] += timedelta(hours=1)
        with pytest.raises(TaskConflict):
            await select_input(ExecutionContext("owner", bad), plan(), source)


@pytest.mark.asyncio
async def test_as_of_uses_exact_point_and_only_truth_after_cutoff_for_metrics(monkeypatch):
    source = Source()
    origin = source.points[300]["timestamp"]
    p = plan(kind="comparison", forecast_origin="as_of", as_of=origin, horizons_s=[10, 30, 1800])
    frozen = await select_input(ExecutionContext("owner", "backtest"), p, source)
    seen = []
    original = ConstantVelocityHeadingPredictor.predict

    def predict(self, request):
        seen.append(request)
        return original(self, request)

    monkeypatch.setattr(ConstantVelocityHeadingPredictor, "predict", predict)
    out = run_trial(frozen, p.model_dump(mode="json"), trial_manifest(p)[0])
    assert len(seen[0].observed) == 200 and seen[0].observed[-1].time == origin
    assert all(v.time <= origin for v in seen[0].observed)
    assert {s["horizon_s"] for s in out["samples"]} == {10, 30}
    assert out["predictions"][0]["points"][-1]["truth_status"] != "matched"
    summary = summarize([out], p.model_dump(mode="json"))
    assert [m["horizon_s"] for m in summary["trials"][0]["metrics"]] == [10, 30]
    off_grid = origin + timedelta(seconds=1.5)
    p2 = plan(kind="comparison", forecast_origin="as_of", as_of=off_grid, horizons_s=[10, 30])
    frozen2 = await select_input(ExecutionContext("owner", "off-grid"), p2, source)
    manifest = frozen2["flights"][0]["observation_windows"][0]
    assert manifest["requested_as_of"] == off_grid.isoformat()
    assert manifest["forecast_origin"] == origin.isoformat()
    assert manifest["observation_lag_s"] == 1.5
    out2 = run_trial(frozen2, p2.model_dump(mode="json"), trial_manifest(p2)[0])
    assert seen[-1].observed[-1].time == origin
    assert seen[-1].target_times == tuple(off_grid + timedelta(seconds=h) for h in [10, 30])
    assert all(p.time <= off_grid for p in seen[-1].observed)
    assert {s["cutoff_time"] for s in out2["samples"]} == {off_grid.isoformat()}
    assert out2["predictions"][0]["horizon_reference"] == "requested_as_of"


@pytest.mark.asyncio
async def test_arbitrary_cutoff_before_first_observation_rejects_without_fabricating_points():
    source = Source()
    before = source.points[0]["timestamp"] - timedelta(seconds=1)
    with pytest.raises(TaskConflict):
        await select_input(
            ExecutionContext("owner", "before"),
            plan(kind="comparison", forecast_origin="as_of", as_of=before),
            source,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forecast", "comparison"])
async def test_no_truth_has_predictions_and_no_error_metrics_in_summary_or_report(kind):
    source = Source()
    options = (
        {}
        if kind == "forecast"
        else {"forecast_origin": "as_of", "as_of": source.points[-1]["timestamp"]}
    )
    p = plan(kind=kind, **options)
    frozen = await select_input(ExecutionContext("owner", "no-truth-" + kind), p, source)
    out = run_trial(frozen, p.model_dump(mode="json"), trial_manifest(p)[0])
    assert out["samples"] == [] and out["prediction_count"] == 3
    summary = summarize([out], p.model_dump(mode="json"))
    assert summary["trials"][0]["metrics"] == []
    assert summary["trials"][0]["phase_metrics"] == []
    assert summary["trials"][0]["worst_cases"] == []
    pages = []
    report, outcome = build(
        p, frozen, [{"workflow_kind": "evaluation", "payload": summary}], [], pages=pages
    )
    assert outcome == "succeeded" and len(pages) == 1
    assert report["context"]["metrics_total_rows"] == 0
    assert all(
        not any(
            c["key"] in {"mae_m", "rmse_m", "ade_m", "fde_m", "error_m"}
            for c in b.get("columns", [])
        )
        for b in report["blocks"]
    )
    assert next(b for b in pages[0]["blocks"] if b["type"] == "trajectory_2d")["points"]


@pytest.mark.asyncio
async def test_real_source_uses_chronological_tail_query_not_bounded_prefix(monkeypatch):
    from scat.infrastructure import database
    from scat.repositories import flight_repository

    points = [
        {
            "timestamp": p["timestamp"],
            "source_index": i + 20324,
            "lat": p["lat"],
            "lon": p["lon"],
            "measured_flight_level": 100,
        }
        for i, p in enumerate(Source(200).points)
    ]
    query = AsyncMock(return_value={"points": points})

    class Repository:
        get_gbar_input_candidates = query
        get_evaluation_inputs = AsyncMock(side_effect=AssertionError("prefix is not tail"))

    monkeypatch.setattr(flight_repository, "ScatFlightRepository", Repository)
    monkeypatch.setattr(database, "flight_database", SimpleNamespace(connect=AsyncMock()))
    frozen = await select_input(ExecutionContext("owner", "large-tail"), plan(), ScatSource())
    query.assert_awaited_once_with(batch="unit", external_id="flight", limit=200)
    window = frozen["flights"][0]["observation_windows"][0]
    assert window["source_first_index"] == 20324 and window["source_last_index"] == 20523


@pytest.mark.asyncio
async def test_future_summary_rejects_fabricated_error_samples():
    p = plan()
    frozen = await select_input(ExecutionContext("owner", "injected-metric"), p, Source())
    out = run_trial(frozen, p.model_dump(mode="json"), trial_manifest(p)[0])
    out["samples"] = [{"error_m": 0}]
    with pytest.raises(ValueError, match="cannot carry"):
        summarize([out], p.model_dump(mode="json"))
