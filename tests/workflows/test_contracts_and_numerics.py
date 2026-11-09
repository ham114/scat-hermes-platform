"""S5 finite proposals, frozen inputs and independent legacy-protocol comparisons."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from application.context import ExecutionContext
from application.task_store import TaskConflict
from scat.experiments.evaluation import EvaluationConfig, aggregate_samples, evaluate_track
from scat.prediction.alignment import build_track
from scat.prediction.baselines import ConstantVelocityHeadingPredictor
from workflow_runtime.catalog import describe, operation_names
from workflow_runtime.contracts import WorkflowProposal, trial_manifest
from workflow_runtime.data import SyntheticSource, load_input, select_input, store_input
from workflow_runtime.numerics import materialize, run_trial, summarize


def proposal(**overrides):
    return WorkflowProposal.model_validate(
        {
            "kind": "comparison",
            "external_ids": ["SYNTH-S5-001", "SYNTH-S5-002"],
            "models": [{"model": "constant-velocity-heading"}, {"model": "kalman-cv"}],
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed": True},
        {"scope": "forged"},
        {"max_flights": 21},
        {"sampling_seed": -1},
        {"repeats": 2},
        {"horizons_s": [float("nan")]},
        {"horizons_s": [60, 10]},
        {"external_ids": ["same", "same"]},
        {"external_ids": ["../escape"]},
        {"models": [{"model": "arbitrary-python-package"}]},
        {"models": [{"model": "kalman-cv", "window_s": 30}]},
        {"models": [{"model": "constant-velocity-heading", "measurement_noise_m": 50}]},
        {"missing_phase": "build"},
        {"min_flights": 4, "max_flights": 2},
    ],
)
def test_invalid(changes):
    with pytest.raises(ValidationError):
        proposal(**changes)


def test_experiment_trial_ids_stable_and_repetitions_distinct():
    plan = proposal(kind="experiment", repeats=3)
    first = trial_manifest(plan)
    assert first == trial_manifest(plan) and len(first) == 6
    assert len({t["trial_id"] for t in first}) == 6
    assert first[0]["configuration"] == first[1]["configuration"]
    assert first[0]["repeat"] != first[1]["repeat"]


def test_tool_groups_and_operation_scopes():
    catalog = describe()
    assert set(catalog["groups"]) == {
        "scat_data_steps",
        "scat_materialization_steps",
        "scat_experiment_steps",
        "scat_evaluation_steps",
        "scat_report_steps",
    }
    assert "scat_materialize" not in operation_names(proposal())
    assert "scat_materialize" in operation_names(proposal(materializations=["phase_segments"]))
    report = WorkflowProposal(kind="report", source_task_ids=("a" * 32,))
    assert operation_names(report) == [
        "scat_report_build",
        "scat_report_read",
        "scat_sample_prepare",
    ]


@pytest.mark.asyncio
async def test_selection_frozen_order_scope_hash_and_private_file(tmp_path):
    context = ExecutionContext("scat-researcher", "workflow-tests")
    plan = proposal()
    frozen = await select_input(context, plan, SyntheticSource())
    repeated = await select_input(context, plan, SyntheticSource())
    assert frozen == repeated
    identity = store_input(tmp_path, frozen)
    assert store_input(tmp_path, repeated) == identity
    assert load_input(tmp_path, identity, scope=context.scope_key) == frozen
    with pytest.raises(TaskConflict):
        load_input(tmp_path, identity, scope=ExecutionContext("other", "workflow-tests").scope_key)
    path = tmp_path / "var/workflow-inputs" / (identity + ".json")
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(TaskConflict):
        load_input(tmp_path, identity)


@pytest.mark.asyncio
async def test_insufficient_samples_and_exact_coverage():
    context = ExecutionContext("scat-researcher", "workflow-tests")
    with pytest.raises(TaskConflict, match="Insufficient"):
        await select_input(
            context, proposal(external_ids=["not-found"], min_flights=1), SyntheticSource()
        )
    frozen = await select_input(
        context, proposal(external_ids=["not-found", "SYNTH-S5-001"]), SyntheticSource()
    )
    assert frozen["coverage"]["selected_count"] == 1
    assert frozen["coverage"]["excluded"] == [{"external_id": "not-found", "reason": "not_found"}]


@pytest.mark.asyncio
async def test_trial_matches_existing_m6_protocol_and_common_sample_accounting():
    plan = proposal()
    frozen = await select_input(
        ExecutionContext("scat-researcher", "oracle"), plan, SyntheticSource()
    )
    outputs = [
        run_trial(frozen, plan.model_dump(mode="json"), trial) for trial in trial_manifest(plan)
    ]
    first_flight = frozen["flights"][0]
    expected, _ = evaluate_track(
        build_track(first_flight["data"]["plots"])[0],
        ConstantVelocityHeadingPredictor(window_s=120),
        EvaluationConfig(
            batch=plan.batch,
            predictor_id="constant-velocity-heading",
            horizons_s=plan.horizons_s,
            stride_s=60,
            min_observed_s=60,
            max_cutoffs_per_flight=4,
        ),
        external_id=first_flight["external_id"],
    )
    actual = [v for v in outputs[0]["samples"] if v["external_id"] == first_flight["external_id"]]
    assert len(actual) == len(expected)
    assert [s["error_m"] for s in actual] == pytest.approx([s.error_m for s in expected], abs=1e-12)
    summary = summarize(outputs, plan.model_dump(mode="json"))
    old_metrics, _ = aggregate_samples(expected, horizons=plan.horizons_s)
    assert all(m.sample_count > 0 for m in old_metrics)
    assert summary["common_sample_count"] == len(outputs[0]["samples"])
    assert all(row["ungrouped_samples"] > 0 for row in summary["trials"])
    broken = {**outputs[1], "samples": [], "status": "failed"}
    partial = summarize([outputs[0], broken], plan.model_dump(mode="json"))
    assert partial["common_sample_count"] == 0 and partial["status"] == "partial"
    assert all(m["sample_count"] == 0 for row in partial["trials"] for m in row["common_metrics"])


@pytest.mark.asyncio
async def test_materialized_phase_and_prediction_metrics_use_real_pure_protocols():
    frozen = await select_input(
        ExecutionContext("scat-researcher", "materialize"), proposal(), SyntheticSource()
    )
    phase = await asyncio.to_thread(
        materialize,
        frozen,
        "phase_segments",
        generated_at=datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
    )
    metrics = await asyncio.to_thread(
        materialize,
        frozen,
        "prediction_metrics",
        generated_at=datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
    )
    assert phase["record_count"] == 2
    assert all(r["point_count"] == 37 and r["segment_count"] > 0 for r in phase["records"])
    assert metrics["record_count"] > 0
    assert all(r["algorithm_version"] == "2" and r["is_valid"] for r in metrics["records"])


@pytest.mark.asyncio
async def test_real_source_repository_contract_and_existing_phase_freeze(monkeypatch):
    import scat.materialization.repository as materialization
    import scat.repositories.flight_repository as flights
    from scat.infrastructure import database
    from scat.materialization import aggregates
    from workflow_runtime.data import ScatSource

    raw = await SyntheticSource().get("SYNTH-S5", "SYNTH-S5-001")
    mongo = object()
    connect = AsyncMock()
    monkeypatch.setattr(database, "flight_database", SimpleNamespace(connect=connect, db=mongo))

    class FlightRepository:
        collection = object()
        get_evaluation_inputs = AsyncMock(return_value=raw)
        get_trajectory_points_for_phases = AsyncMock(return_value=raw["phase_points"])
        get_trajectory_phase_stats = AsyncMock(return_value=raw["phase_stats"])
        get_summary_by_source = AsyncMock(return_value={"identity": raw["identity"]})

    class Materializations:
        def __init__(self, mongo_database):
            assert mongo_database is mongo

        async def find_latest(self, kind, key):
            assert kind == "phase_segments" and key["algorithm_version"] == "1"
            return {
                "segments": [
                    {
                        "phase": "climb",
                        "start_time": "2020-01-01T00:00:00+00:00",
                        "end_time": "2020-01-01T00:06:00+00:00",
                    }
                ]
            }

    monkeypatch.setattr(flights, "ScatFlightRepository", FlightRepository)
    monkeypatch.setattr(materialization, "MaterializationRepository", Materializations)
    monkeypatch.setattr(
        aggregates, "fetch_flight_feature_basis", AsyncMock(return_value=raw["feature_basis"])
    )
    source = ScatSource()
    result = await source.get("SYNTH-S5", "SYNTH-S5-001")
    assert result["existing_phase_segments"][0]["phase"] == "climb"
    connect.assert_awaited_once()
    frozen = await select_input(
        ExecutionContext("scat-researcher", "existing-phases"),
        proposal(missing_phase="require"),
        source,
    )
    assert frozen["coverage"]["selected_count"] == 2
