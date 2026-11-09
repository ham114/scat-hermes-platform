"""M9 query-index audit, explain inspection, and small benchmark helpers."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any, Final

from core.settings import settings
from scat.repositories.flight_repository import SCAT_SOURCE_SYSTEM
from uav.repositories.uav_repository import UAV_SOURCE_SYSTEM

IndexSpec = tuple[str, tuple[tuple[str, int], ...]]

SCAT_QUERY_INDEXES: Final[tuple[IndexSpec, ...]] = (
    (
        "idx_scat_system_batch_iobt_external",
        (
            ("source.system", 1),
            ("source.batch", 1),
            ("identity.iobt_utc", 1),
            ("source.external_id", 1),
        ),
    ),
    (
        "idx_scat_system_batch_external",
        (("source.system", 1), ("source.batch", 1), ("source.external_id", 1)),
    ),
    ("idx_scat_system_batch_cursor", (("source.system", 1), ("source.batch", 1), ("_id", 1))),
    (
        "idx_scat_system_batch_callsign_external",
        (
            ("source.system", 1),
            ("source.batch", 1),
            ("identity.callsign", 1),
            ("source.external_id", 1),
        ),
    ),
    ("idx_scat_system_candidate", (("source.system", 1), ("identity.candidate_key", 1))),
)

UAV_QUERY_INDEXES: Final[tuple[IndexSpec, ...]] = (
    ("idx_uav_system_external", (("source.system", 1), ("source.external_id", 1))),
    (
        "idx_uav_system_takeoff_external",
        (("source.system", 1), ("identity.takeoff_at", 1), ("source.external_id", 1)),
    ),
    (
        "idx_uav_system_model_takeoff_external",
        (
            ("source.system", 1),
            ("source.model", 1),
            ("identity.takeoff_at", 1),
            ("source.external_id", 1),
        ),
    ),
    (
        "idx_uav_system_kind_takeoff_external",
        (
            ("source.system", 1),
            ("identity.flight_kind", 1),
            ("identity.takeoff_at", 1),
            ("source.external_id", 1),
        ),
    ),
)


async def ensure_query_indexes(database: Any) -> list[str]:
    """Create indexes derived from repository filters/sorts; safe and idempotent."""
    created: list[str] = []
    for collection_name, specs in (
        (settings.SCAT_FLIGHTS_COLLECTION, SCAT_QUERY_INDEXES),
        (settings.UAV_FLIGHTS_COLLECTION, UAV_QUERY_INDEXES),
    ):
        collection = database[collection_name]
        for name, keys in specs:
            created.append(await collection.create_index(list(keys), name=name))
    return created


def summarize_explain(name: str, explain: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Mongo explain tree to stable regression fields."""
    stages: set[str] = set()
    indexes: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            stage = value.get("stage")
            if isinstance(stage, str):
                stages.add(stage)
            index_name = value.get("indexName")
            if isinstance(index_name, str):
                indexes.add(index_name)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(explain.get("queryPlanner", {}).get("winningPlan", {}))
    execution = explain.get("executionStats") or {}
    return {
        "name": name,
        "ok": "COLLSCAN" not in stages,
        "stages": sorted(stages),
        "indexes": sorted(indexes),
        "n_returned": int(execution.get("nReturned") or 0),
        "docs_examined": int(execution.get("totalDocsExamined") or 0),
        "keys_examined": int(execution.get("totalKeysExamined") or 0),
        "execution_ms": float(execution.get("executionTimeMillis") or 0.0),
    }


async def explain_critical_queries(
    database: Any,
    *,
    batch: str,
    external_id: str = "__m9_probe__",
) -> list[dict[str, Any]]:
    """Explain representative bounded find paths without returning business data."""
    probes = (
        (
            "scat_flight_lookup",
            settings.SCAT_FLIGHTS_COLLECTION,
            {
                "filter": {
                    "source.system": SCAT_SOURCE_SYSTEM,
                    "source.batch": batch,
                    "source.external_id": external_id,
                },
                "projection": {"_id": 1},
                "limit": 1,
            },
        ),
        (
            "scat_batch_list",
            settings.SCAT_FLIGHTS_COLLECTION,
            {
                "filter": {"source.system": SCAT_SOURCE_SYSTEM, "source.batch": batch},
                "projection": {"_id": 0, "source.external_id": 1},
                "sort": {"identity.iobt_utc": 1, "source.external_id": 1},
                "limit": 20,
            },
        ),
        (
            "scat_batch_cursor",
            settings.SCAT_FLIGHTS_COLLECTION,
            {
                "filter": {"source.system": SCAT_SOURCE_SYSTEM, "source.batch": batch},
                "projection": {"_id": 1, "source.external_id": 1},
                "sort": {"_id": 1},
                "limit": 200,
            },
        ),
        (
            "uav_time_list",
            settings.UAV_FLIGHTS_COLLECTION,
            {
                "filter": {"source.system": UAV_SOURCE_SYSTEM},
                "projection": {"_id": 0, "source.external_id": 1},
                "sort": {"identity.takeoff_at": 1, "source.external_id": 1},
                "limit": 20,
            },
        ),
    )
    reports: list[dict[str, Any]] = []
    for name, collection, body in probes:
        command = {"explain": {"find": collection, **body}, "verbosity": "executionStats"}
        reports.append(summarize_explain(name, await database.command(command)))
    return reports


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile for dependency-free benchmark reports."""
    if not values:
        raise ValueError("values must not be empty")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(samples_ms: Iterable[float]) -> dict[str, float | int]:
    values = list(samples_ms)
    if not values:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    return {
        "count": len(values),
        "p50_ms": round(percentile(values, 0.5), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


__all__ = [
    "SCAT_QUERY_INDEXES",
    "UAV_QUERY_INDEXES",
    "ensure_query_indexes",
    "explain_critical_queries",
    "latency_summary",
    "percentile",
    "summarize_explain",
]
