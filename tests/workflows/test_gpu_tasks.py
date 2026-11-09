import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from unittest.mock import Mock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from application.context import ExecutionContext
from application.task_store import TaskConflict, digest
from gbar_core.transport import FEATURES, INPUT_UNITS, OUTPUT_UNITS, schema_digest
from scat_model_runtime.backends.base import (
    BackendError,
    BackendHealth,
    InferenceRequest,
    InferenceResult,
)
from scat_model_runtime.backends.gbar_codec import GbarTransportCodec
from scat_model_runtime.task_io import (
    RegressionPredictionSeries,
    RegressionTaskInput,
    RegressionTaskOutput,
)
from tests.scat.preprocessing.test_gbar_v35 import _points
from workflow_runtime.contracts import WorkflowProposal, trial_manifest
from workflow_runtime.data import select_input
from workflow_runtime.gpu import (
    GPURegistration,
    GPURequests,
    freeze_windows,
    registration,
    run_gpu_trial,
)
from workflow_runtime.numerics import run_trial, summarize
from workflow_runtime.report import build


@pytest.mark.asyncio
async def test_async_preparation_probes_sync_backend_in_worker_thread(tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path
    from uuid import uuid4

    from application.skill_job_factory import create_skill_tasks
    from tests.hermes_runtime.test_skill_jobs_s4 import Artifacts, Runs

    root = Path(__file__).resolve().parents[2]
    config = GPURegistration.model_validate(config_body())

    class HealthBackend:
        def health(self):
            async def value():
                return BackendHealth(True)

            return asyncio.run(value())

        def close(self):
            pass

    monkeypatch.setattr("workflow_runtime.gpu.registration", lambda *a, **k: config)
    monkeypatch.setattr(GPURegistration, "backend", lambda self: HealthBackend())
    app = create_skill_tasks(
        root, tmp_path / "tasks.sqlite", runs=Runs(), artifacts=Artifacts(), workflows_enabled=True
    )
    app.workflow_source = DenseSource()
    view = await app.prepare_workflow(
        ExecutionContext("owner", "health-thread-" + uuid4().hex), proposal(), proposal_key="health"
    )
    try:
        assert view["confirmation_required"] and view["workflow"]["gpu"] == config.pin()
    finally:
        (root / "var/workflow-inputs" / (view["workflow"]["input_sha256"] + ".json")).unlink()


def config_body():
    return {
        "enabled": True,
        "transport": {
            "endpoint": "http://127.0.0.1:18127",
            "release_id": "scat-gbar-v23:test-pinned",
        },
        "provenance": {
            **dict.fromkeys(
                ("checkpoint_sha256", "config_sha256", "source_tree_sha256", "runtime_sha256"),
                "a" * 64,
            ),
            "schema_sha256": schema_digest(),
            "source_uri": "project-internal://synthetic-reviewed",
        },
    }


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config/production-gpu.json"
    path.parent.mkdir()
    path.write_text(json.dumps(config_body()), encoding="utf-8")
    monkeypatch.setenv("SCAT_GPU_CONFIG_FILE", "config/production-gpu.json")
    return tmp_path, registration(tmp_path, required=True)


class DenseSource:
    version = "synthetic-dense-gpu-v1"
    synthetic = True

    async def candidates(self, batch, limit):
        return ["synthetic-a", "synthetic-b"][:limit]

    async def get(self, batch, external_id):
        return {
            "plots": [
                {
                    "timestamp": p["timestamp"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "altitude_m": p["measured_flight_level"] * 30.48,
                }
                for p in _points(241)
            ]
        }

    async def gbar_points(self, batch, external_id, cutoff):
        return [p for p in _points(241) if datetime.fromisoformat(p["timestamp"]) <= cutoff][-500:]


def proposal(**changes):
    return WorkflowProposal.model_validate(
        {
            "kind": "comparison",
            "batch": "synthetic-gpu",
            "external_ids": ["synthetic-a", "synthetic-b"],
            "models": [
                {"model": "scat-gbar-v23", "version": "23"},
                {"model": "constant-velocity-heading"},
            ],
            "min_observed_s": 350,
            "max_cutoffs_per_flight": 2,
            "max_flights": 2,
            **changes,
        }
    )


class LinearGPU:
    def __init__(self, config):
        self.config, self.calls = config, 0

    def health(self):
        return BackendHealth(True)

    def close(self):
        pass

    def execute(self, request):
        self.calls += 1
        source = RegressionTaskInput.model_validate(request.payload)
        predictions = []
        for s in source.series:
            values = tuple(
                (
                    s.values[-1][0] + i * 0.002,
                    s.values[-1][1] + i * 0.002,
                    9144.0,
                    100.0,
                    50.0,
                    3.048,
                )
                for i in range(1, 37)
            )
            predictions.append(
                RegressionPredictionSeries(
                    input_ref=s.input_ref,
                    forecast_origin=s.timestamps[-1],
                    time_offsets_seconds=tuple(float(i * 10) for i in range(1, 37)),
                    values=values,
                    expected_values=values,
                    annotations={
                        "observed_trajectory": [
                            {
                                "time_step_s": float((t - s.timestamps[-1]).total_seconds()),
                                "lat": v[0],
                                "lon": v[1],
                                "altitude": 9144.0,
                            }
                            for t, v in zip(s.timestamps[-71::2], s.values[-71::2], strict=True)
                        ]
                    },
                )
            )
        output = RegressionTaskOutput(
            model_key="scat-gbar-v23",
            model_version="23",
            target_names=FEATURES,
            target_units=OUTPUT_UNITS,
            series=tuple(predictions),
        )
        return InferenceResult(
            **{
                k: getattr(request, k)
                for k in (
                    "request_id",
                    "run_id",
                    "model_key",
                    "model_version",
                    "input_schema",
                    "output_schema",
                )
            },
            payload=output.model_dump(mode="json"),
            provenance=self.config.provenance.model_dump(mode="json"),
        )


@pytest_asyncio.fixture
async def frozen():
    p = proposal()
    source = DenseSource()
    data = await select_input(ExecutionContext("owner", "thread"), p, source)
    assert await freeze_windows(data, p, source) == 4
    return p, data


def request(frozen, identity="request-1"):
    _, data = frozen
    source = RegressionTaskInput(
        feature_names=FEATURES,
        feature_units=INPUT_UNITS,
        series=[f["gbar_windows"][0] for f in data["flights"]],
    )
    return InferenceRequest(
        request_id=identity,
        run_id="run-1",
        model_key="scat-gbar-v23",
        model_version="23",
        input_schema=source.schema_version,
        output_schema="regression-task-output/v1",
        payload=source.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"models": [{"model": "scat-gbar-v23"}]},
        {"models": [{"model": "scat-gbar-v23", "version": "23", "window_s": 120}]},
        {"models": [{"model": "kalman-cv", "version": "23"}]},
        {"min_observed_s": 60},
        {"horizons_s": [600]},
        {"max_cutoffs_per_flight": 5},
        {"selection": "random"},
    ],
)
def test_gpu_contract_rejects_unreviewed_versions_parameters_and_bounds(change):
    with pytest.raises(ValidationError):
        proposal(**change)


def test_registration_default_disabled_pin_and_path_guard(config):
    root, c = config
    assert registration(root, expected=c.pin()) == c
    (root / "config/production-gpu.json").write_text('{"enabled":false}')
    assert registration(root) is None
    with pytest.raises(TaskConflict, match="not enabled"):
        registration(root, required=True)
    changed = config_body()
    changed["state_file"] = "var/elsewhere/requests.sqlite"
    (root / "config/production-gpu.json").write_text(json.dumps(changed))
    with pytest.raises(TaskConflict, match="ledger"):
        registration(root)


def test_cuda_health_is_required_without_changing_other_backend_modes(config):
    _, c = config
    meta = {
        "contract_version": "gbar-v23-metadata/v1",
        "model_key": "scat-gbar-v23",
        "model_version": "23",
        "input_schema": "regression-task-input/v1",
        "output_schema": "scat-gbar-v23-prediction/v1",
        "release_id": c.transport.release_id,
        "provenance": c.provenance.model_dump(mode="json"),
        "ready": True,
        "state": "ready",
        "device": "cpu",
        "max_batch_size": 8,
        "max_series_points": 500,
        "max_total_points": 2000,
    }
    assert GbarTransportCodec(c.provenance).verify_metadata(meta, c.transport.release_id).ready
    assert (
        not GbarTransportCodec(c.provenance, required_device="cuda")
        .verify_metadata(meta, c.transport.release_id)
        .ready
    )
    meta["device"] = "cuda"
    assert (
        GbarTransportCodec(c.provenance, required_device="cuda")
        .verify_metadata(meta, c.transport.release_id)
        .ready
    )


@pytest.mark.asyncio
async def test_seeded_server_sampling_freezes_pool_and_gpu_windows():
    p = proposal(selection="random", external_ids=[], max_flights=1)
    source = DenseSource()
    a = await select_input(ExecutionContext("u", "t"), p, source)
    b = await select_input(ExecutionContext("u", "t"), p, source)
    assert a == b and len(a["flights"]) == 1 and a["coverage"]["candidate_count"] == 2
    await freeze_windows(a, p, source)
    assert len(a["flights"][0]["gbar_windows"]) == 2


@pytest.mark.asyncio
async def test_wrong_cutoff_window_fails_before_any_gpu_send():
    source = DenseSource()
    source.gbar_points = Mock(wraps=source.gbar_points)
    p = proposal(min_observed_s=350)
    data = await select_input(ExecutionContext("u", "t"), p, source)

    async def wrong(*args):
        return _points(70)

    source.gbar_points = wrong
    with pytest.raises(TaskConflict, match="No model-ready"):
        await freeze_windows(data, p, source)


@pytest.mark.asyncio
async def test_persistent_receipt_replay_and_tamper_rejection(config, frozen):
    root, c = config
    backend = LinearGPU(c)
    ledger = GPURequests(root, c)
    req = request(frozen)
    first = ledger.execute(backend, req, task="task", execution="unit")
    again = GPURequests(root, c).execute(backend, req, task="task", execution="unit")
    assert first == again and backend.calls == 1 and ledger.records()[0]["calls"] == 1
    with closing(ledger.connect()) as db, db:
        db.execute("UPDATE gpu_requests SET output='{}'")
    with pytest.raises(TaskConflict, match="integrity"):
        ledger.execute(backend, req, task="task", execution="unit")
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_unknown_blocks_all_jobs_and_never_retries(config, frozen):
    root, c = config
    ledger = GPURequests(root, c)
    req = request(frozen)
    backend = Mock()
    backend.execute.side_effect = BackendError("inference_timeout", execution_state="unknown")
    with pytest.raises(BackendError):
        ledger.execute(backend, req, task="task", execution="unit")
    with pytest.raises(BackendError):
        ledger.execute(backend, req, task="task", execution="unit")
    with pytest.raises(BackendError):
        ledger.execute(backend, request(frozen, "different"), task="other", execution="other")
    assert backend.execute.call_count == 1
    row = ledger.records()[0]
    with pytest.raises(TaskConflict):
        ledger.resolve(row["id"], row["request_sha"])
    ledger.resolve(row["id"], row["request_sha"], remote_termination_confirmed=True)
    with pytest.raises(BackendError):
        ledger.execute(backend, req, task="task", execution="unit")
    assert backend.execute.call_count == 1 and ledger.records()[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_lost_caller_remains_unknown_until_operator_resolution(config, frozen):
    root, c = config
    ledger = GPURequests(root, c)
    req = request(frozen, "lost-caller")
    with closing(ledger.connect()) as db, db:
        db.execute(
            "INSERT INTO gpu_requests(id,task,execution,request_sha,state,pid,birth,calls,created) VALUES(?,?,?,?,'running',99999999,'old-birth',1,0)",
            (req.request_id, "task", "unit", digest(req.model_dump(mode="json"))),
        )
    row = ledger.records()[0]
    assert row["state"] == "unknown"
    backend = Mock()
    with pytest.raises(BackendError):
        ledger.execute(backend, req, task="task", execution="unit")
    backend.execute.assert_not_called()
    ledger.resolve(req.request_id, row["request_sha"], remote_termination_confirmed=True)
    assert ledger.records()[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_concurrent_gpu_admission_and_authorization_before_send(config, frozen):
    root, c = config
    ledger = GPURequests(root, c)
    req = request(frozen)
    backend = LinearGPU(c)
    started = threading.Event()
    release = threading.Event()
    execute = backend.execute

    def blocking(r):
        started.set()
        assert release.wait(5)
        return execute(r)

    backend.execute = blocking
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ledger.execute, backend, req, task="task", execution="unit")
        assert started.wait(5)
        try:
            with pytest.raises(BackendError):
                ledger.execute(backend, request(frozen, "second"), task="other", execution="other")
        finally:
            release.set()
        first.result()
    assert backend.calls == 1

    def denied():
        raise TaskConflict("expired lease")

    with pytest.raises(TaskConflict):
        ledger.execute(
            backend, request(frozen, "third"), task="other", execution="other", authorize=denied
        )
    assert len(ledger.records()) == 1


@pytest.mark.asyncio
async def test_gpu_metrics_common_axis_graph_pages_and_no_recompute(config, frozen, monkeypatch):
    root, c = config
    p, data = frozen
    backend = LinearGPU(c)
    monkeypatch.setattr(GPURegistration, "backend", lambda self: backend)
    trials = trial_manifest(p)
    row = {
        "task_id": "task-gpu",
        "run_id": "run-gpu",
        "snapshot": {"workflow": {"gpu": c.pin(), "proposal": p.model_dump(mode="json")}},
    }
    output = run_gpu_trial(root, row, "execution-1", trials[0], data)
    assert output["status"] == "succeeded" and len(output["samples"]) == 12
    assert max(s["error_m"] for s in output["samples"]) < 1e-6
    baseline = run_trial(data, p.model_dump(mode="json"), trials[1])
    summary = summarize([output, baseline], p.model_dump(mode="json"))
    assert summary["common_sample_count"] == 12
    assert summary["prediction_pages"][0]["context"]["gpu_provenance_sha256"] == digest(
        c.pin()["provenance"]
    )
    pages = []
    report, _ = build(
        p, data, [{"workflow_kind": "evaluation", "payload": summary}], [], pages=pages
    )
    assert report["context"]["report_page_count"] >= 1
    assert any(page["context"].get("gpu_release") == c.transport.release_id for page in pages)
    assert run_gpu_trial(root, row, "execution-1", trials[0], data) == output
    assert backend.calls == 1
    changed = config_body()
    changed["transport"]["release_id"] = "scat-gbar-v23:other"
    (root / "config/production-gpu.json").write_text(json.dumps(changed))
    with pytest.raises(TaskConflict, match="changed"):
        run_gpu_trial(root, row, "execution-2", trials[0], data)
    assert backend.calls == 1
