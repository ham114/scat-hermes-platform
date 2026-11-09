"""Exact GPU input and missing-truth semantics with the existing synthetic transport."""

import json
from unittest.mock import AsyncMock

import pytest

from application.context import ExecutionContext
from scat_model_runtime.backends.base import InferenceResult
from tests.scat.preprocessing.test_gbar_v35 import _points
from tests.workflows.test_gpu_tasks import DenseSource, LinearGPU, config_body
from workflow_runtime.contracts import WorkflowProposal, trial_manifest
from workflow_runtime.data import select_input
from workflow_runtime.gpu import GPURegistration, registration, run_gpu_trial
from workflow_runtime.numerics import summarize


def test_gbar_uses_model_specific_observation_default_but_keeps_explicit_limits():
    from pydantic import ValidationError

    proposal = {
        "kind": "forecast",
        "external_ids": ["flight"],
        "models": [{"model": "scat-gbar-v23", "version": "23"}],
        "observation_last_n": 200,
    }
    assert WorkflowProposal.model_validate(proposal).min_observed_s == 350
    with pytest.raises(ValidationError):
        WorkflowProposal.model_validate({**proposal, "min_observed_s": 60})
    cpu = {**proposal, "models": [{"model": "constant-velocity-heading"}]}
    assert WorkflowProposal.model_validate(cpu).min_observed_s == 60


def test_workflow_validation_feedback_identifies_gbar_limits_without_echoing_values(monkeypatch):
    from pathlib import Path

    from pydantic import ValidationError

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from run_workflow_tool import validation_feedback

    with pytest.raises(ValidationError) as caught:
        WorkflowProposal.model_validate(
            {
                "kind": "forecast",
                "external_ids": ["private-input-never-echo"],
                "models": [{"model": "scat-gbar-v23", "version": "23"}],
                "observation_last_n": 200,
                "min_observed_s": 60,
            }
        )
    result = validation_feedback(caught.value)
    assert result[0]["rule"] == "gbar_window_limits" and "min_observed_s" in result[0]["field"]
    assert "input" not in str(result)
    assert "private-input-never-echo" not in str(result)


class ForecastSource(DenseSource):
    async def forecast_input(self, batch, external_id, last_n):
        features = _points(241)[-last_n:]
        return {
            "plots": [
                {
                    "timestamp": p["timestamp"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "altitude_m": p["measured_flight_level"] * 30.48,
                }
                for p in features
            ],
            "feature_points": features,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("truth_representation", ["empty", "null_rows", "unexpected"])
async def test_gpu_uses_exact_200_frozen_rows_never_expands_context_and_forecast_needs_no_truth(
    tmp_path, monkeypatch, truth_representation
):
    config_path = tmp_path / "config/production-gpu.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(config_body()))
    monkeypatch.setenv("SCAT_GPU_CONFIG_FILE", "config/production-gpu.json")
    config = registration(tmp_path, required=True)
    p = WorkflowProposal.model_validate(
        {
            "kind": "forecast",
            "batch": "synthetic-gpu",
            "external_ids": ["synthetic-a"],
            "models": [{"model": "scat-gbar-v23", "version": "23"}],
            "observation_last_n": 200,
            "min_observed_s": 350,
            "horizons_s": [10, 30, 60],
            "max_cutoffs_per_flight": 1,
        }
    )
    source = ForecastSource()
    source.gbar_points = AsyncMock(side_effect=AssertionError("Do not read more context"))
    frozen = await select_input(ExecutionContext("owner", "gpu-future"), p, source)
    window = frozen["flights"][0]["gbar_windows"][0]
    assert len(window["timestamps"]) == 200
    assert window["source_indices"] == list(range(41, 241))
    assert window["annotations"]["actual_future_candidates"] == []
    assert window["annotations"]["raw_observation_points"] == 200
    assert window["annotations"]["model_resampled_points"] == 36
    source.gbar_points.assert_not_awaited()

    class NoTruthGPU(LinearGPU):
        def execute(self, request):
            original = super().execute(request)
            payload = original.payload
            for series in payload["series"]:
                series["expected_values"] = (
                    []
                    if truth_representation == "empty"
                    else [[None] * len(values) for values in series["values"]]
                )
                if truth_representation == "unexpected":
                    series["expected_values"][0][0] = 1.0
            return InferenceResult(**{**original.model_dump(), "payload": payload})

    backend = NoTruthGPU(config)
    monkeypatch.setattr(GPURegistration, "backend", lambda self: backend)
    row = {
        "task_id": "gpu-future",
        "run_id": "gpu-run",
        "snapshot": {"workflow": {"proposal": p.model_dump(mode="json"), "gpu": config.pin()}},
    }
    if truth_representation == "unexpected":
        from scat_model_runtime.backends.base import BackendError

        with pytest.raises(BackendError) as caught:
            run_gpu_trial(tmp_path, row, "execution", trial_manifest(p)[0], frozen)
        assert caught.value.code == "gpu_unapproved_future_truth"
        assert backend.calls == 1
        return
    out = run_gpu_trial(tmp_path, row, "execution", trial_manifest(p)[0], frozen)
    assert out["status"] == "succeeded" and out["samples"] == []
    assert out["prediction_count"] == 3 and backend.calls == 1
    assert out["analysis_result"]["context"]["metric_availability"] == "no_verified_truth"
    assert all(
        not any(
            c["key"] in {"mae", "rmse", "ade_metres", "fde_metres"} for c in b.get("columns", [])
        )
        for b in out["analysis_result"]["blocks"]
    )
    summary = summarize([out], p.model_dump(mode="json"))
    assert summary["trials"][0]["metrics"] == []
    repeated = run_gpu_trial(tmp_path, row, "execution", trial_manifest(p)[0], frozen)
    assert repeated == out and backend.calls == 1
