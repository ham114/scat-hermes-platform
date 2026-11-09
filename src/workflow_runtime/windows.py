"""Exact observation windows, frozen before approval; future truth is never model input."""

from datetime import UTC, timedelta
from itertools import pairwise

from application.task_store import TaskConflict, digest
from scat.experiments.evaluation import EvaluationConfig, cutoff_times
from scat.prediction.alignment import build_track


def prediction_windows(flight, proposal):
    track, duplicates = build_track(flight["data"]["plots"])
    exact = proposal.observation_last_n
    if duplicates or len(track) != len(flight["data"]["plots"]):
        raise TaskConflict(
            "Invalid or duplicate observation points; exact windows cannot drop rows"
        )
    if proposal.kind == "forecast":
        cutoffs = [track[-1].time] if track else []
    elif proposal.forecast_origin == "as_of":
        moment = proposal.as_of.astimezone(UTC)
        cutoffs = [moment]
    else:
        cutoffs = cutoff_times(
            track,
            EvaluationConfig(
                batch=proposal.batch,
                predictor_id="approved-window",
                horizons_s=proposal.horizons_s,
                stride_s=proposal.stride_s,
                min_observed_s=0 if exact else proposal.min_observed_s,
                min_observed_points=exact or 3,
                max_cutoffs_per_flight=proposal.max_cutoffs_per_flight,
                max_actual_gap_s=proposal.max_track_gap_s,
            ),
        )
    windows = []
    for cutoff in cutoffs:
        available = [p for p in track if p.time <= cutoff]
        if exact and len(available) < exact:
            raise TaskConflict(f"Insufficient observation points: {len(available)}/{exact}")
        observed = tuple(available[-exact:] if exact else available)
        if len(observed) < 3:
            raise TaskConflict("Insufficient observations at or before as_of")
        if any(
            (b.time - a.time).total_seconds() > proposal.max_track_gap_s
            for a, b in pairwise(observed)
        ):
            raise TaskConflict("Observation gap exceeds approved maximum")
        windows.append(
            (observed, tuple(cutoff + timedelta(seconds=h) for h in proposal.horizons_s))
        )
    return windows


def window_manifest(flight, proposal):
    result = []
    raw = flight["data"].get("feature_points", [])
    for observed, targets in prediction_windows(flight, proposal):
        rows = [
            {
                "timestamp": p.time.isoformat(),
                "lat": p.lat,
                "lon": p.lon,
                "altitude_m": p.altitude_m,
            }
            for p in observed
        ]
        indices = [p["source_index"] for p in raw] if raw else []
        result.append(
            {
                "external_id": flight["external_id"],
                "mode": "future_forecast" if proposal.kind == "forecast" else "historical_backtest",
                "requested_points": proposal.observation_last_n,
                "point_count": len(observed),
                "start_time": observed[0].time.isoformat(),
                "forecast_origin": observed[-1].time.isoformat(),
                "span_s": (observed[-1].time - observed[0].time).total_seconds(),
                "observation_sha256": digest(rows),
                "source_indices_sha256": digest(indices) if indices else None,
                "source_first_index": indices[0] if indices else None,
                "source_last_index": indices[-1] if indices else None,
                "target_times": [t.isoformat() for t in targets],
            }
        )
        if proposal.forecast_origin == "as_of" and proposal.as_of != observed[-1].time:
            result[-1].update(
                requested_as_of=proposal.as_of.astimezone(UTC).isoformat(),
                last_observation_time=observed[-1].time.isoformat(),
                horizon_reference="requested_as_of",
                observation_lag_s=(proposal.as_of - observed[-1].time).total_seconds(),
            )
    return result


def verified_windows(flight, proposal):
    if flight.get("observation_windows") != window_manifest(flight, proposal):
        raise TaskConflict("Approved observation window changed")
    return prediction_windows(flight, proposal)
