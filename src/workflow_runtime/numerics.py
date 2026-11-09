"""Reuse registered CPU predictors and frozen M6/M4 pure numerical protocols."""

from __future__ import annotations

import asyncio
from datetime import datetime

from scat.experiments.evaluation import (
    EvaluationConfig,
    SampleResult,
    aggregate_phase_metrics,
    aggregate_samples,
    evaluate_track,
)
from scat.experiments.grouping import phase_for_time
from scat.prediction.alignment import build_track
from scat.prediction.baselines import (
    ConstantVelocityHeadingPredictor,
    KalmanConstantVelocityPredictor,
)
from workflow_runtime.contracts import TrialConfiguration, WorkflowProposal


def predictor(configuration):
    config = TrialConfiguration.model_validate(configuration)
    if config.model == "constant-velocity-heading":
        return ConstantVelocityHeadingPredictor(window_s=config.window_s or 120)
    if config.model != "kalman-cv":
        raise ValueError("GPU models require the approved durable GPU backend")
    return KalmanConstantVelocityPredictor(
        process_noise_mps2=config.process_noise_mps2,
        measurement_noise_m=config.measurement_noise_m,
    )


def run_trial(frozen, proposal, trial):
    p = WorkflowProposal.model_validate(proposal)
    if p.kind == "forecast" or p.observation_last_n or p.forecast_origin == "as_of":
        from workflow_runtime.forecast import run_windowed_trial

        return run_windowed_trial(frozen, p, trial)
    model = predictor(trial["configuration"])
    config = EvaluationConfig(
        batch=p.batch,
        predictor_id=model.predictor_id,
        horizons_s=p.horizons_s,
        stride_s=p.stride_s,
        min_observed_s=p.min_observed_s,
        max_cutoffs_per_flight=p.max_cutoffs_per_flight,
        max_actual_gap_s=p.max_track_gap_s,
    )
    records, failed, exclusions = [], [], {}
    for flight in frozen["flights"]:
        try:
            samples, excluded = evaluate_track(
                build_track(flight["data"]["plots"])[0],
                model,
                config,
                external_id=flight["external_id"],
            )
            records.extend(
                {
                    "external_id": s.external_id,
                    "cutoff_time": s.cutoff_time.isoformat(),
                    "horizon_s": s.horizon_s,
                    "error_m": s.error_m,
                }
                for s in samples
            )
            for key, count in excluded.items():
                exclusions[key] = exclusions.get(key, 0) + count
        except (ValueError, ArithmeticError):
            failed.append({"external_id": flight["external_id"], "reason": "predictor_rejected"})
            if p.failure_policy == "stop":
                break
    return {
        "trial_id": trial["trial_id"],
        "configuration": trial["configuration"],
        "repeat": trial["repeat"],
        "protocol": "m6-eval-v1",
        "units": "m",
        "samples": records,
        "failed_flights": failed,
        "exclusions": exclusions,
        "selected_flights": len(frozen["flights"]),
        "status": "partial" if failed and records else ("succeeded" if records else "failed"),
    }


def sample_key(sample):
    return sample["external_id"], sample["cutoff_time"], sample["horizon_s"]


def samples(values):
    return [
        SampleResult(
            external_id=v["external_id"],
            cutoff_time=datetime.fromisoformat(v["cutoff_time"]),
            horizon_s=v["horizon_s"],
            error_m=v["error_m"],
        )
        for v in values
    ]


def summarize(outputs, proposal, *, phases=None):
    p = WorkflowProposal.model_validate(proposal)
    if p.kind == "forecast" and any(o.get("samples") for o in outputs):
        raise ValueError("Future forecasts cannot carry historical error samples")
    usable = [o for o in outputs if o.get("samples")]
    common = (
        set.intersection(*(set(map(sample_key, o["samples"])) for o in usable)) if usable else set()
    )
    # A failed approved model has no comparable samples; it cannot disappear from the denominator.
    if len(usable) != len(outputs):
        common = set()
    rows = []
    phases = phases or {}
    for output in outputs:
        own = samples(output.get("samples", []))
        shared = samples([s for s in output.get("samples", []) if sample_key(s) in common])
        metrics, worst = aggregate_samples(own, horizons=p.horizons_s, worst_limit=p.worst_cases)
        comparable, _ = aggregate_samples(shared, horizons=p.horizons_s, worst_limit=0)
        grouped, ungrouped = aggregate_phase_metrics(
            own, phase_of=lambda s: phase_for_time(phases.get(s.external_id, []), s.cutoff_time)
        )
        if p.missing_phase == "require" and ungrouped:
            raise ValueError("Approved evaluation requires complete phase coverage")
        rows.append(
            {
                "trial_id": output["trial_id"],
                "status": output["status"],
                "configuration": output["configuration"],
                "repeat": output["repeat"],
                "metrics": [v.model_dump(mode="json") for v in metrics if v.sample_count],
                "common_metrics": [v.model_dump(mode="json") for v in comparable if v.sample_count],
                "phase_metrics": [v.model_dump(mode="json") for v in grouped],
                "ungrouped_samples": ungrouped,
                "worst_cases": [v.model_dump(mode="json") for v in worst],
                "failed_flights": output.get("failed_flights", []),
                "exclusions": output.get("exclusions", {}),
                "prediction_count": output.get("prediction_count", 0),
                "metric_availability": "available" if own else "no_verified_truth",
            }
        )
    return {
        "protocol": "point-exact-prediction/v1"
        if p.kind == "forecast" or p.observation_last_n or p.as_of
        else "m6-eval-v1",
        "prediction_mode": "future_forecast" if p.kind == "forecast" else "historical_backtest",
        "prediction_count": sum(o.get("prediction_count", 0) for o in outputs),
        "prediction_pages": [page for o in outputs for page in o.get("analysis_results", [])]
        + [o["analysis_result"] for o in outputs if "analysis_result" in o],
        "units": "m",
        "trial_count": len(outputs),
        "common_sample_count": len(common),
        "trials": rows,
        "status": "succeeded"
        if rows and all(r["status"] == "succeeded" for r in rows)
        else "partial",
    }


def materialize(frozen, kind, *, generated_at, proposal=None, prerequisites=None):
    from application.task_store import digest
    from scat.materialization.contracts import BatchStatisticsRecord
    from scat.materialization.tasks import (
        build_flight_features_record,
        build_prediction_metric_summary_records,
        materialize_flight_phase_segments,
        materialize_prediction_metrics,
    )
    from scat.prediction.alignment import HORIZONS_S

    allowed = {
        "batch_statistics",
        "flight_features",
        "phase_segments",
        "prediction_metrics",
        "prediction_metric_summaries",
    }
    if kind not in allowed:
        raise ValueError("Unregistered materialization")
    version = frozen["source_version"] + ":sample:" + digest(frozen)[0:24]
    moment = datetime.fromisoformat(generated_at)
    prerequisites = prerequisites or {}

    class FrozenRepository:
        def __init__(self, flight):
            self.flight = flight

        async def get_trajectory_points_for_phases(self, **kwargs):
            return self.flight["data"].get("phase_points", [])

        async def get_evaluation_inputs(self, **kwargs):
            return self.flight["data"]

    class Collector:
        def __init__(self):
            self.rows = []

        async def upsert_record(self, name, record):
            self.rows.append(record.model_dump(mode="json"))
            return 1

        async def upsert_records(self, name, records):
            self.rows.extend(r.model_dump(mode="json") for r in records)
            return len(records)

    async def compute():
        collector = Collector()
        failures, empty, succeeded = [], [], []
        if kind == "batch_statistics":
            flights = frozen["flights"]
            identities = [f["data"].get("identity", {}) for f in flights]
            counts = [len(f["data"]["plots"]) for f in flights]
            snapshots = [len(f["data"].get("snapshots", [])) for f in flights]

            def unique(key):
                return len({v[key] for v in identities if v.get(key)})

            def missing(key):
                return sum(not v.get(key) for v in identities)

            record = BatchStatisticsRecord(
                batch=frozen["batch"],
                source_version=version,
                algorithm_version="1",
                generated_at=moment,
                flight_count=len(flights),
                unique_callsign_count=unique("callsign"),
                aircraft_type_count=unique("aircraft_type"),
                departure_airport_count=unique("adep"),
                arrival_airport_count=unique("ades"),
                total_track_points=sum(counts),
                average_track_points=sum(counts) / len(counts),
                minimum_track_points=min(counts),
                maximum_track_points=max(counts),
                flights_with_prediction=sum(v > 0 for v in snapshots),
                total_prediction_snapshots=sum(snapshots),
                missing_callsign_count=missing("callsign"),
                missing_aircraft_type_count=missing("aircraft_type"),
                missing_departure_airport_count=missing("adep"),
                missing_arrival_airport_count=missing("ades"),
                missing_iobt_count=missing("iobt"),
                empty_trajectory_count=sum(v == 0 for v in counts),
                flights_without_prediction_count=sum(v == 0 for v in snapshots),
            )
            await collector.upsert_record(kind, record)
            succeeded.extend(f["external_id"] for f in flights)
        elif kind == "prediction_metric_summaries":
            if (
                not {"prediction_metrics", "phase_segments", "flight_features"}
                <= prerequisites.keys()
            ):
                raise ValueError("Verified prerequisite materializations required")
            per_flight = {}
            for r in prerequisites["prediction_metrics"]["records"]:
                if not r["is_valid"]:
                    continue
                key = (r["external_id"], r["horizon_s"])
                bucket = per_flight.setdefault(
                    key,
                    {
                        "_id": {"external_id": key[0], "horizon_s": key[1]},
                        "count": 0,
                        **{
                            k: 0.0
                            for k in (
                                "sum_h",
                                "sum_h2",
                                "sum_abs_v",
                                "sum_v2",
                                "sum_abs_along",
                                "sum_abs_cross",
                            )
                        },
                    },
                )
                h, v = r["horizontal_error_m"], r["vertical_error_m"]
                bucket["count"] += 1
                for k, value in {
                    "sum_h": h,
                    "sum_h2": h * h,
                    "sum_abs_v": abs(v or 0),
                    "sum_v2": (v or 0) ** 2,
                    "sum_abs_along": abs(r["along_track_error_m"] or 0),
                    "sum_abs_cross": abs(r["cross_track_error_m"] or 0),
                }.items():
                    bucket[k] += value
            records = build_prediction_metric_summary_records(
                per_flight=list(per_flight.values()),
                phase_map={
                    r["external_id"]: r["dominant_phase"]
                    for r in prerequisites["phase_segments"]["records"]
                },
                aircraft_map={
                    r["external_id"]: r["aircraft_type"]
                    for r in prerequisites["flight_features"]["records"]
                },
                percentiles={},
                batch=frozen["batch"],
                source_version=version,
                generated_at=moment,
            )
            await collector.upsert_records(kind, records)
            succeeded.extend(f["external_id"] for f in frozen["flights"])
            # Mongo approximate percentiles are deliberately absent in this bounded CPU summary.
            if any(p["status"] != "succeeded" for p in prerequisites.values()):
                failures.append({"reason": "partial_prerequisite"})
        else:
            for flight in frozen["flights"]:
                before = len(collector.rows)
                try:
                    if kind == "flight_features":
                        data = flight["data"]
                        basis = data.get("feature_basis")
                        if not basis:
                            raise ValueError("Feature basis unavailable")
                        record = build_flight_features_record(
                            batch=frozen["batch"],
                            external_id=flight["external_id"],
                            basis=basis,
                            phase_rows=data.get("phase_points", []),
                            phases=data.get("phase_stats", []),
                            source_version=version,
                            generated_at=moment,
                        )
                        await collector.upsert_record(kind, record)
                    else:
                        operation = (
                            materialize_flight_phase_segments
                            if kind == "phase_segments"
                            else materialize_prediction_metrics
                        )
                        await operation(
                            batch=frozen["batch"],
                            external_id=flight["external_id"],
                            flight_repository=FrozenRepository(flight),
                            materialization_repository=collector,
                            source_version=version,
                            generated_at=moment,
                        )
                    # M5 CFPL horizons stay frozen at HORIZONS_S, separate from M6 model horizons.
                    succeeded.append(flight["external_id"])
                    if len(collector.rows) == before:
                        empty.append(flight["external_id"])
                except (ValueError, ArithmeticError, TypeError):
                    del collector.rows[before:]
                    failures.append(
                        {"external_id": flight["external_id"], "reason": "derived_input_rejected"}
                    )
                    if proposal and proposal["failure_policy"] == "stop":
                        break
        return {
            "kind": kind,
            "records": collector.rows,
            "record_count": len(collector.rows),
            "source_version": version,
            "scope": "approved_frozen_sample",
            "selected_flights": len(frozen["flights"]),
            "succeeded_flights": succeeded,
            "failed_flights": failures,
            "empty_flights": empty,
            "unprocessed_count": max(
                0,
                len(frozen["flights"]) - len(succeeded) - sum("external_id" in f for f in failures),
            ),
            "horizons_s": list(HORIZONS_S)
            if kind in {"prediction_metrics", "prediction_metric_summaries"}
            else None,
            "percentiles": "unavailable" if kind == "prediction_metric_summaries" else None,
            "status": "partial"
            if failures and collector.rows
            else ("failed" if failures else "succeeded"),
        }

    return asyncio.run(compute())
