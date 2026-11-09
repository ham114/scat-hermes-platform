"""Pinned GBAR HTTP execution with a shared, conservative GPU request ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from application.task_store import TaskConflict, canonical, digest
from gbar_core.transport import FEATURES, INPUT_UNITS, ModelProvenance
from hermes_runtime.skills import checked_path
from scat_model_runtime.backends.base import BackendError, InferenceRequest, InferenceResult
from scat_model_runtime.backends.bento_http import BentoHttpBackend, BentoHttpConfig
from scat_model_runtime.backends.gbar_codec import GbarTransportCodec
from scat_model_runtime.task_io import RegressionTaskInput, RegressionTaskOutput


class GPURegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: Literal[True]
    transport: BentoHttpConfig
    provenance: ModelProvenance
    state_file: str = "var/gpu/requests.sqlite"
    max_windows_per_job: int = Field(default=16, ge=1, le=32)

    def backend(self):
        # A disconnected send is accounted as a single attempt; never retry a POST here.
        config = self.transport.model_copy(update={"max_connect_retries": 0})
        return BentoHttpBackend(config, GbarTransportCodec(self.provenance, required_device="cuda"))

    def pin(self):
        return {
            "registration_sha256": digest(self.model_dump(mode="json")),
            "release_id": self.transport.release_id,
            "provenance": self.provenance.model_dump(mode="json"),
            "device": "cuda",
        }


def registration(root, *, required=False, expected=None):
    relative = os.environ.get("SCAT_GPU_CONFIG_FILE", "config/production-gpu.json")
    path = checked_path(Path(root).resolve(), relative)
    if not path.is_file():
        config = None
    else:
        if path.stat().st_size > 16384:
            raise TaskConflict("GPU registration exceeds size limit")
        body = json.loads(path.read_text(encoding="utf-8"))
        if body == {"enabled": False}:
            config = None
        else:
            config = GPURegistration.model_validate(body)
            # One configured ledger is shared across all task databases on this host.
            ledger = checked_path(Path(root).resolve(), config.state_file)
            if not ledger.is_relative_to(Path(root).resolve() / "var/gpu"):
                raise TaskConflict("GPU ledger must be in the project var/gpu directory")
    if (required or expected is not None) and config is None:
        raise TaskConflict("Production GPU model is not enabled")
    if expected is not None and config.pin() != expected:
        raise TaskConflict("Approved GPU registration changed")
    return config


class GPURequests:
    def __init__(self, root, config):
        self.path = checked_path(Path(root).resolve(), config.state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS gpu_requests(
                id TEXT PRIMARY KEY, task TEXT NOT NULL, execution TEXT NOT NULL,
                request_sha TEXT NOT NULL, state TEXT NOT NULL, pid INTEGER,
                birth TEXT, calls INTEGER NOT NULL DEFAULT 0, output TEXT,
                output_sha TEXT, error TEXT, created REAL NOT NULL, finished REAL)""")
            db.execute("CREATE INDEX IF NOT EXISTS gpu_request_state ON gpu_requests(state)")
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def records(self):
        from application.skill_cpu_backend import process_start

        with closing(self.connect()) as db, db:
            for row in db.execute(
                "SELECT id,pid,birth FROM gpu_requests WHERE state='running'"
            ).fetchall():
                if row["birth"] is None or process_start(row["pid"]) != row["birth"]:
                    db.execute(
                        "UPDATE gpu_requests SET state='unknown',error='caller_lost_remote_outcome_unknown' WHERE id=? AND state='running'",
                        (row["id"],),
                    )
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,task,execution,request_sha,state,calls,error,created,finished "
                    "FROM gpu_requests ORDER BY created DESC LIMIT 100"
                )
            ]

    def execute(self, backend, request, *, task, execution, authorize=lambda: None):
        from application.skill_cpu_backend import process_start

        sha = digest(request.model_dump(mode="json"))
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM gpu_requests WHERE id=?", (request.request_id,)
            ).fetchone()
            if old is not None:
                if (
                    old["request_sha"] != sha
                    or old["task"] != task
                    or old["execution"] != execution
                ):
                    raise TaskConflict("GPU request identity conflict")
                if old["state"] == "succeeded":
                    value = json.loads(old["output"])
                    if digest(value) != old["output_sha"]:
                        raise TaskConflict("GPU result integrity failed")
                    result = InferenceResult.model_validate(value)
                    result.verify_identity(request)
                    return result
                raise BackendError("gpu_request_" + old["state"], execution_state="unknown")
            if db.execute(
                "SELECT 1 FROM gpu_requests WHERE state IN ('running','unknown')"
            ).fetchone():
                raise BackendError("gpu_resource_busy")
            if db.execute("SELECT count(*) FROM gpu_requests").fetchone()[0] >= 10000:
                raise BackendError("gpu_receipt_capacity")
            authorize()
            db.execute(
                "INSERT INTO gpu_requests(id,task,execution,request_sha,state,pid,birth,calls,created) "
                "VALUES(?,?,?,?,'running',?,?,1,?)",
                (
                    request.request_id,
                    task,
                    execution,
                    sha,
                    os.getpid(),
                    process_start(os.getpid()),
                    time.time(),
                ),
            )
        try:
            result = backend.execute(request)
            result.verify_identity(request)
        except BaseException as exc:
            unknown = not isinstance(exc, BackendError) or exc.execution_state != "not_started"
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE gpu_requests SET state=?,error=?,finished=? WHERE id=?",
                    (
                        "unknown" if unknown else "failed",
                        exc.code if isinstance(exc, BackendError) else "gpu_response_unknown",
                        time.time(),
                        request.request_id,
                    ),
                )
            raise
        value = result.model_dump(mode="json")
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE gpu_requests SET state='succeeded',output=?,output_sha=?,finished=? WHERE id=?",
                (canonical(value), digest(value), time.time(), request.request_id),
            )
        return result

    def resolve(self, identity, expected, *, remote_termination_confirmed=False):
        """Trusted operator only: terminal failure, never reset or replay the request."""
        from application.skill_cpu_backend import process_start

        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gpu_requests WHERE id=?", (identity,)).fetchone()
            if row is None or row["request_sha"] != expected:
                raise TaskConflict("GPU receipt precondition failed")
            if row["state"] not in {"running", "unknown"} or not remote_termination_confirmed:
                raise TaskConflict("Trusted remote termination acknowledgement required")
            if row["state"] == "running" and process_start(row["pid"]) == row["birth"]:
                raise TaskConflict("GPU caller is still active")
            db.execute(
                "UPDATE gpu_requests SET state='failed',error='operator_verified_termination',finished=? WHERE id=?",
                (time.time(), identity),
            )


async def freeze_windows(frozen, proposal, source):
    from gbar_core.preprocessing import ScatGbarDataError, ScatGbarV35DataProcessor
    from scat.prediction.alignment import build_track
    from scat_model_runtime.contracts import ModelInputReference
    from scat_model_runtime.resolvers.scat_gbar import ScatGbarRegressionInputResolver as Resolver
    from scat_model_runtime.task_io import RegressionInputSeries
    from workflow_runtime.windows import prediction_windows

    count = 0
    for flight in frozen["flights"]:
        track = build_track(flight["data"]["plots"])[0]
        windows, excluded = [], []
        for observed, _ in prediction_windows(flight, proposal):
            cutoff = observed[-1].time
            points = (
                flight["data"].get("feature_points", [])
                if proposal.kind == "forecast"
                else await source.gbar_points(proposal.batch, flight["external_id"], cutoff)
            )
            if len(points) > 500:
                raise TaskConflict("GPU source window exceeds limit")
            try:
                if proposal.observation_last_n:
                    points = points[-proposal.observation_last_n :]
                    comparable = build_track(
                        [
                            {
                                "timestamp": p.get("timestamp"),
                                "lat": p.get("lat"),
                                "lon": p.get("lon"),
                                "altitude_m": p.get("measured_flight_level") * 30.48
                                if isinstance(p.get("measured_flight_level"), (int, float))
                                else None,
                            }
                            for p in points
                        ]
                    )[0]
                    if tuple(comparable) != observed or len(points) != proposal.observation_last_n:
                        raise ValueError("GPU raw input differs from exact approved observations")
                prepared = ScatGbarV35DataProcessor().prepare(
                    points, batch=proposal.batch, external_id=flight["external_id"]
                )
                if prepared.timestamps[-1] != cutoff:
                    raise ValueError("GPU origin differs from the common evaluation cutoff")
                series = RegressionInputSeries(
                    input_ref=ModelInputReference(
                        dataset="scat",
                        batch=proposal.batch,
                        external_id=flight["external_id"],
                        end_time=cutoff,
                        limit=len(points),
                    ),
                    timestamps=tuple(Resolver._timestamp(p.get("timestamp")) for p in points),
                    values=tuple(
                        tuple(Resolver._optional_float(p.get(k)) for k in FEATURES) for p in points
                    ),
                    source_indices=tuple(
                        Resolver._source_index(p.get("source_index"), i)
                        for i, p in enumerate(points)
                    ),
                    annotations={
                        "prediction_mode": "future_forecast"
                        if proposal.kind == "forecast"
                        else "historical_backtest",
                        "raw_observation_points": len(points),
                        "model_resampled_points": len(prepared.timestamps),
                        "actual_future_candidates": [
                            {
                                "timestamp": p.time.isoformat(),
                                "lat": p.lat,
                                "lon": p.lon,
                                "measured_flight_level": p.altitude_m,
                            }
                            for p in track
                            if proposal.kind != "forecast" and p.time > cutoff
                        ][:200],
                    },
                )
                windows.append(series.model_dump(mode="json"))
            except (ScatGbarDataError, ValueError):
                excluded.append(
                    {"cutoff_time": cutoff.isoformat(), "reason": "gbar_window_rejected"}
                )
        flight["gbar_windows"], flight["gbar_excluded"] = windows, excluded
        count += len(windows)
    if not count:
        raise TaskConflict("No model-ready GBAR windows at the approved common cutoffs")
    return count


def run_gpu_trial(
    root, row, execution, trial, frozen, *, authorize=lambda: None, stopped=lambda: False
):
    from agents.analysis_results import build_regression_model_analysis
    from scat.prediction.alignment import build_track, haversine_metres, interpolate_track
    from scat_model_runtime.regression_evaluators import RegressionErrorEvaluator
    from scat_model_runtime.task_io import RegressionInputSeries
    from workflow_runtime.contracts import WorkflowProposal

    plan = row["snapshot"]["workflow"]
    proposal = WorkflowProposal.model_validate(plan["proposal"])
    config = registration(root, required=True, expected=plan["gpu"])
    backend = config.backend()
    ledger = GPURequests(root, config)
    windows = [
        RegressionInputSeries.model_validate(w)
        for f in frozen["flights"]
        for w in f.get("gbar_windows", [])
    ]
    if not 1 <= len(windows) <= config.max_windows_per_job:
        raise TaskConflict("Invalid approved GPU window count")
    predictions, receipts = [], []
    try:
        health = backend.health()
        if not health.ready:
            raise BackendError(health.code)
        for index in range(0, len(windows), 4):
            if stopped():
                raise BackendError("gpu_cancelled_before_next_request")
            source = RegressionTaskInput(
                feature_names=FEATURES,
                feature_units=INPUT_UNITS,
                series=tuple(windows[index : index + 4]),
            )
            identity = digest({"execution": execution, "chunk": index // 4})
            request = InferenceRequest(
                request_id=identity,
                run_id=row["run_id"],
                model_key="scat-gbar-v23",
                model_version="23",
                input_schema=source.schema_version,
                output_schema="regression-task-output/v1",
                payload=source.model_dump(mode="json"),
            )
            response = ledger.execute(
                backend, request, task=row["task_id"], execution=execution, authorize=authorize
            )
            output = RegressionTaskOutput.model_validate(response.payload)
            if (
                output.model_key != "scat-gbar-v23"
                or output.model_version != "23"
                or output.target_names != FEATURES
            ):
                raise BackendError("invalid_response", execution_state="unknown")
            for original, predicted in zip(source.series, output.series, strict=True):
                if (
                    original.input_ref != predicted.input_ref
                    or predicted.forecast_origin != original.timestamps[-1]
                ):
                    raise BackendError("gpu_forecast_origin_mismatch", execution_state="unknown")
            predictions.extend(output.series)
            receipts.append({"request_id": identity, "provenance": response.provenance})
    finally:
        backend.close()
    result = RegressionTaskOutput(
        model_key="scat-gbar-v23",
        model_version="23",
        target_names=FEATURES,
        target_units=output.target_units,
        series=tuple(predictions),
    )
    if proposal.kind == "forecast":
        cropped = []
        for series in result.series:
            if any(
                v is not None for row in series.expected_values for v in row
            ) or series.annotations.get("actual_future_trajectory"):
                raise BackendError("gpu_unapproved_future_truth", execution_state="unknown")
            indices = [series.time_offsets_seconds.index(h) for h in proposal.horizons_s]
            cropped.append(
                series.model_copy(
                    update={
                        "time_offsets_seconds": proposal.horizons_s,
                        "values": tuple(series.values[i] for i in indices),
                        "expected_values": (),
                    }
                )
            )
        result = result.model_copy(update={"series": tuple(cropped)})
        predictions = list(result.series)
    records, exclusions = [], {}
    by_id = {f["external_id"]: f for f in frozen["flights"]}
    for series in predictions:
        flight = by_id[series.input_ref.external_id]
        actual = build_track(flight["data"]["plots"])[0]
        predicted = build_track(
            [
                {
                    "timestamp": series.forecast_origin + timedelta(seconds=t),
                    "lat": v[0],
                    "lon": v[1],
                    "altitude_m": v[2],
                }
                for t, v in zip(series.time_offsets_seconds, series.values, strict=True)
            ]
        )[0]
        for horizon in () if proposal.kind == "forecast" else proposal.horizons_s:
            reference_time = proposal.as_of or series.forecast_origin
            target = reference_time + timedelta(seconds=horizon)
            a, _ = interpolate_track(actual, target, max_gap_s=proposal.max_track_gap_s)
            p, _ = interpolate_track(predicted, target, max_gap_s=30)
            if a is None or p is None:
                exclusions["unavailable_truth_or_prediction"] = (
                    exclusions.get("unavailable_truth_or_prediction", 0) + 1
                )
                continue
            records.append(
                {
                    "external_id": series.input_ref.external_id,
                    "cutoff_time": reference_time.isoformat(),
                    "horizon_s": horizon,
                    "error_m": haversine_metres(p.lat, p.lon, a.lat, a.lon),
                }
            )
    rejected = sum(len(f.get("gbar_excluded", [])) for f in frozen["flights"])
    exclusions["gbar_window_rejected"] = rejected
    # A forecast can be valid without future truth; metrics then remain unavailable.
    evaluation = None
    if proposal.kind != "forecast":
        try:
            evaluation = RegressionErrorEvaluator().evaluate(output=result)
        except ValueError:
            pass
    analysis = build_regression_model_analysis(result, evaluation).model_dump(mode="json")
    analysis["context"].update(
        synthetic=frozen["synthetic"],
        prediction_mode="future_forecast" if proposal.kind == "forecast" else "historical_backtest",
        metric_availability="available" if evaluation is not None else "no_verified_truth",
        gpu_release=plan["gpu"]["release_id"],
        gpu_provenance_sha256=digest(plan["gpu"]["provenance"]),
        **{"gpu_" + k: v for k, v in plan["gpu"]["provenance"].items()},
        **(
            {
                "requested_as_of": proposal.as_of.isoformat(),
                "horizon_reference": "requested_as_of",
                "native_grid_metrics_reference": "last_observation",
            }
            if proposal.as_of
            else {}
        ),
    )
    from schema.analysis import AnalysisResult

    analysis = AnalysisResult.model_validate(analysis).model_dump(mode="json")
    return {
        "trial_id": trial["trial_id"],
        "configuration": trial["configuration"],
        "repeat": trial["repeat"],
        "protocol": "point-exact-prediction/v1"
        if proposal.kind == "forecast" or proposal.observation_last_n or proposal.as_of
        else "m6-eval-v1",
        "prediction_mode": "future_forecast"
        if proposal.kind == "forecast"
        else "historical_backtest",
        "prediction_count": sum(len(s.values) for s in result.series),
        "units": "m",
        "samples": records,
        "failed_flights": [],
        "exclusions": exclusions,
        "selected_flights": len(frozen["flights"]),
        "status": "partial"
        if rejected and predictions
        else "succeeded"
        if predictions
        else "failed",
        "gpu_requests": receipts,
        "analysis_result": analysis,
    }
