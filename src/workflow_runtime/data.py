"""Frozen, bounded server-side inputs. Raw tracks never appear in workflow tool replies."""

from __future__ import annotations

import json
import math
import random
import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from application.task_store import TaskConflict, canonical, digest


def json_value(value):
    def encode(obj):
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                raise ValueError("Input timestamps must include timezone")
            return obj.astimezone(UTC).isoformat()
        raise TypeError("Unsupported input value")

    return json.loads(json.dumps(value, default=encode, allow_nan=False))


def unexpired(value):
    if value is None:
        return True
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment > datetime.now(UTC)
    except ValueError:
        return False


def store_input(root, body):
    identity = digest(body)
    root = Path(root).resolve()
    directory = root / "var/workflow-inputs"
    if (
        directory.is_symlink()
        or (root / "var").is_symlink()
        or not directory.resolve().is_relative_to(root)
    ):
        raise TaskConflict("Input directory link rejected")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (identity + ".json")
    raw = canonical(body).encode()
    if len(raw) > 32 * 1024 * 1024:
        raise TaskConflict("Frozen sample exceeds size ceiling")
    try:
        with path.open("xb") as stream:
            path.chmod(0o600)
            stream.write(raw)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != raw:
            raise TaskConflict("Frozen sample conflict")
    return identity


def load_input(root, identity, *, scope=None):
    if not re.fullmatch("[0-9a-f]{64}", identity):
        raise TaskConflict("Invalid frozen sample identity")
    directory = Path(root).resolve() / "var/workflow-inputs"
    path = directory / (identity + ".json")
    if (
        directory.is_symlink()
        or (Path(root).resolve() / "var").is_symlink()
        or not directory.resolve().is_relative_to(Path(root).resolve())
        or path.is_symlink()
        or not path.is_file()
    ):
        raise TaskConflict("Frozen sample unavailable")
    body = json.loads(path.read_text(encoding="utf-8"))
    if digest(body) != identity or (scope and body["scope"] != scope):
        raise TaskConflict("Frozen sample integrity or ownership changed")
    return body


class SyntheticSource:
    version = "synthetic-workflow-v1"
    synthetic = True

    async def candidates(self, batch, limit):
        return [f"SYNTH-S5-{i:03d}" for i in range(1, 5)][:limit] if batch == "SYNTH-S5" else []

    async def forecast_input(self, batch, external_id, last_n):
        body = await self.get(batch, external_id)
        if body is None:
            return None
        return {"plots": body["plots"][-last_n:], "snapshots": []}

    async def get(self, batch, external_id):
        if batch != "SYNTH-S5" or external_id not in {f"SYNTH-S5-{i:03d}" for i in range(1, 5)}:
            return None
        offset = int(external_id[-3:])
        start = datetime(2020, 1, 1, tzinfo=UTC)
        points = [
            {
                "timestamp": start + timedelta(seconds=i * 10),
                "lat": 30 + offset / 100 + i * 0.001,
                "lon": 110 + i * 0.001 + (i - 15) ** 2 * 0.00001,
                "altitude_m": 3000 + min(i, 15) * 10,
            }
            for i in range(37)
        ]
        snapshots = [
            {
                "time_stamp": points[6]["timestamp"],
                "route": [
                    {
                        "eto": p["timestamp"],
                        "lat": p["lat"] + 0.0001,
                        "lon": p["lon"] - 0.0001,
                        "afl_value": p["altitude_m"] / 30.48,
                        "afl_unit": "F",
                    }
                    for p in points[6:]
                ],
            }
        ]
        phases = [
            {
                "timestamp": p["timestamp"],
                "measured_flight_level": p["altitude_m"] / 30.48,
                "lat": p["lat"],
                "lon": p["lon"],
                "vertical_mode": 1 if i < 15 else 0,
            }
            for i, p in enumerate(points)
        ]
        from scat.prediction.alignment import build_track, haversine_metres

        track = build_track(points)[0]
        return {
            "plots": points,
            "snapshots": snapshots,
            "phase_points": phases,
            "feature_basis": {
                "aircraft_type": "SYNTH-A320",
                "point_count": len(points),
                "duration_s": 360,
                "distance_m": sum(
                    haversine_metres(a.lat, a.lon, b.lat, b.lon) for a, b in pairwise(track)
                ),
            },
            "identity": {
                "callsign": external_id,
                "aircraft_type": "SYNTH-A320",
                "adep": "SYN1",
                "ades": "SYN2",
                "iobt": True,
            },
            "phase_stats": [
                {
                    "point_count": len(points),
                    "average_ground_speed_mps": 15.0,
                    "maximum_ground_speed_mps": 17.0,
                    "min_flight_level": 3000 / 30.48,
                    "max_flight_level": 3150 / 30.48,
                }
            ],
        }


class ScatSource:
    version = "scat-import-snapshot-v1"
    synthetic = False

    def __init__(self):
        from scat.repositories.flight_repository import ScatFlightRepository

        self.repository = ScatFlightRepository()

    async def candidates(self, batch, limit):
        from scat.infrastructure.database import flight_database

        await flight_database.connect()
        rows = await self.repository.search_flights(batch=batch, limit=limit, min_track_points=171)
        return [str(row["source"]["external_id"]) for row in rows]

    async def forecast_input(self, batch, external_id, last_n):
        from scat.infrastructure.database import flight_database

        await flight_database.connect()
        # Query the chronological tail itself: the old 20k-prefix read is not a tail.
        body = await self.repository.get_gbar_input_candidates(
            batch=batch, external_id=external_id, limit=last_n
        )
        if body is None:
            return None
        features = body.get("points", [])
        plots = [
            {
                "timestamp": p.get("timestamp"),
                "lat": p.get("lat"),
                "lon": p.get("lon"),
                "altitude_m": (
                    p["measured_flight_level"] * 30.48
                    if isinstance(p.get("measured_flight_level"), int | float)
                    and not isinstance(p["measured_flight_level"], bool)
                    else None
                ),
            }
            for p in features
        ]
        return {"plots": plots, "feature_points": features, "snapshots": []}

    async def gbar_points(self, batch, external_id, cutoff):
        body = await self.repository.get_gbar_input_candidates(
            batch=batch,
            external_id=external_id,
            end_time=cutoff.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds"),
            limit=500,
        )
        return body.get("points", []) if body else []

    async def get(self, batch, external_id):
        from scat.infrastructure.database import flight_database

        await flight_database.connect()
        body = await self.repository.get_evaluation_inputs(batch=batch, external_id=external_id)
        if body is not None:
            body["phase_points"] = await self.repository.get_trajectory_points_for_phases(
                batch=batch, external_id=external_id
            )
            from scat.materialization.aggregates import fetch_flight_feature_basis

            body["feature_basis"] = await fetch_flight_feature_basis(
                self.repository.collection, batch=batch, external_id=external_id
            )
            body["phase_stats"] = await self.repository.get_trajectory_phase_stats(
                batch=batch, external_id=external_id
            )
            summary = (
                await self.repository.get_summary_by_source(batch=batch, external_id=external_id)
                or {}
            )
            meta = summary.get("identity") or {}
            body["identity"] = {
                key: meta.get(key) for key in ("callsign", "aircraft_type", "adep", "ades")
            }
            body["identity"]["iobt"] = bool(meta.get("iobt"))
            from scat.materialization.repository import MaterializationRepository
            from scat.materialization.tasks import (
                DEFAULT_SOURCE_VERSION,
                FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
            )

            existing = await MaterializationRepository(flight_database.db).find_latest(
                "phase_segments",
                {
                    "batch": batch,
                    "external_id": str(external_id),
                    "source_version": DEFAULT_SOURCE_VERSION,
                    "algorithm_version": FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
                },
            )
            if existing and unexpired(existing.get("expires_at")):
                body["existing_phase_segments"] = existing.get("segments", [])
        return body


async def select_input(context, proposal, source):
    from scat.prediction.alignment import build_track

    candidates = sorted(
        set(
            await source.candidates(proposal.batch, proposal.candidate_limit)
            if proposal.selection == "random"
            else proposal.external_ids
        )
    )
    if len(candidates) > 100 or any(
        not isinstance(v, str) or not 1 <= len(v) <= 100 or any(c in v for c in "/\\\x00")
        for v in candidates
    ):
        raise TaskConflict("Invalid server candidate list")
    random.Random(proposal.sampling_seed).shuffle(candidates)
    selected, excluded = [], []
    for external_id in candidates:
        if len(selected) >= proposal.max_flights:
            break
        raw = (
            await source.forecast_input(proposal.batch, external_id, proposal.observation_last_n)
            if proposal.kind == "forecast"
            else await source.get(proposal.batch, external_id)
        )
        reason = None
        if raw is None:
            reason = "not_found"
        else:
            raw = json_value(raw)
            track, duplicate_timestamps = build_track(raw.get("plots") or [])
            if len(raw.get("plots") or []) >= 20000:
                reason = "bounded_input_may_be_truncated"
            elif (
                proposal.kind == "forecast"
                and len(raw.get("plots") or []) != proposal.observation_last_n
            ):
                reason = "insufficient_exact_observation_points"
            elif duplicate_timestamps:
                reason = "duplicate_timestamps"
            elif len(track) < 3:
                reason = "insufficient_points"
            elif (
                proposal.kind != "forecast"
                and not proposal.observation_last_n
                and proposal.forecast_origin != "as_of"
                and (track[-1].time - track[0].time).total_seconds()
                < proposal.min_observed_s + min(proposal.horizons_s)
            ):
                reason = "insufficient_observation_and_future"
            elif (
                not proposal.observation_last_n
                and proposal.forecast_origin != "as_of"
                and any(
                    (b.time - a.time).total_seconds() > proposal.max_track_gap_s
                    for a, b in pairwise(track)
                )
            ):
                reason = "track_gap_exceeds_approved_limit"
            elif any(
                not math.isfinite(p.lat)
                or not math.isfinite(p.lon)
                or not -90 <= p.lat <= 90
                or not -180 <= p.lon <= 180
                for p in track
            ):
                reason = "invalid_coordinates"
        if reason:
            excluded.append({"external_id": external_id, "reason": reason})
        else:
            candidate = {"external_id": external_id, "data": raw, "data_sha256": digest(raw)}
            if proposal.models:
                from workflow_runtime.windows import window_manifest

                try:
                    candidate["observation_windows"] = window_manifest(candidate, proposal)
                    if not candidate["observation_windows"]:
                        raise TaskConflict("No eligible observation windows")
                except TaskConflict as exc:
                    excluded.append({"external_id": external_id, "reason": str(exc)})
                    continue
            if any(m.model == "scat-gbar-v23" for m in proposal.models):
                from workflow_runtime.gpu import freeze_windows

                try:
                    await freeze_windows({"flights": [candidate]}, proposal, source)
                except TaskConflict as exc:
                    if not str(exc).startswith("No model-ready GBAR"):
                        raise
                    excluded.append(
                        {"external_id": external_id, "reason": "no_model_ready_gpu_window"}
                    )
                    continue
            selected.append(candidate)
    if len(selected) < proposal.min_flights:
        reasons = "; ".join(sorted({item["reason"] for item in excluded}))
        raise TaskConflict(
            f"Insufficient eligible samples: {len(selected)}/{proposal.min_flights}: {reasons}"
        )
    if proposal.missing_phase == "require" and any(
        not f["data"].get("existing_phase_segments") for f in selected
    ):
        raise TaskConflict(
            "Required existing phase coverage is unavailable; explicitly approve phase construction"
        )
    return {
        "schema_version": "workflow-input/v1",
        "scope": context.scope_key,
        "batch": proposal.batch,
        "source_version": source.version,
        "synthetic": source.synthetic,
        "sampling_seed": proposal.sampling_seed,
        "candidate_ids": candidates,
        "flights": selected,
        "coverage": {
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "checked_count": len(selected) + len(excluded),
            "excluded": excluded,
            "unexamined_count": len(candidates) - len(selected) - len(excluded),
        },
    }
