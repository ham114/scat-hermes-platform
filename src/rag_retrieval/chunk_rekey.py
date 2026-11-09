"""Re-key chunk ids from one corpus generation to the next.

A chunk id is a hash of source, version, locator and text, so every corpus generation change
invalidates ids held by evaluation artifacts. Most old chunks survive inside exactly one new
chunk, because the new generation writes larger, sentence-aligned blocks. This module maps
those automatically and reports the cases it cannot decide, instead of guessing.

The mapping is deliberately conservative: an id is only re-keyed when the whole old text is
found inside a single new chunk of the same source. Everything else is reported for review.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

# A gram shorter than this is not distinctive enough to prove containment.
GRAM_SIZE = 40
GRAM_STEP = 8
# Coverage above which a new chunk is worth showing to a reviewer as a candidate mapping.
PARTIAL_REVIEW_THRESHOLD = 0.5

UNCHANGED = "unchanged"
EXACT = "exact"
PARTIAL = "partial"
UNMAPPED = "unmapped"
# A reviewer settled this id by hand. Kept distinct from EXACT so a report can always tell an
# automatic mapping apart from a human judgement.
REVIEWED = "reviewed"


def flatten(text: str) -> str:
    """Whitespace-insensitive form, because chunking only moves whitespace around."""

    return "".join(str(text).split())


def gram_coverage(needle: str, haystack: str) -> float:
    """Fraction of the needle's overlapping grams that appear in the haystack."""

    if not needle:
        return 0.0
    if len(needle) <= GRAM_SIZE:
        return 1.0 if needle in haystack else 0.0
    grams = [
        needle[start : start + GRAM_SIZE]
        for start in range(0, len(needle) - GRAM_SIZE + 1, GRAM_STEP)
    ]
    if not grams:
        return 0.0
    return sum(1 for gram in grams if gram in haystack) / len(grams)


def map_chunk_id(
    old_id: str,
    old_chunks: Mapping[str, Mapping[str, Any]],
    new_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Map one old chunk id onto the new generation, or explain why it cannot be mapped."""

    old = old_chunks.get(old_id)
    if old is None:
        return {"old_id": old_id, "status": UNMAPPED, "reason": "old chunk not found"}

    source_candidates = list(new_by_source.get(old["source_id"], []))
    if any(candidate["chunk_id"] == old_id for candidate in source_candidates):
        return {"old_id": old_id, "status": UNCHANGED, "targets": [old_id]}

    needle = flatten(old["text"])
    exact = [c for c in source_candidates if needle in flatten(c["text"])]
    if len(exact) == 1:
        return {"old_id": old_id, "status": EXACT, "targets": [exact[0]["chunk_id"]]}
    if len(exact) > 1:
        return {
            "old_id": old_id,
            "status": PARTIAL,
            "targets": [c["chunk_id"] for c in exact],
            "reason": f"old text is contained in {len(exact)} new chunks",
        }

    scored = sorted(
        (
            (gram_coverage(needle, flatten(candidate["text"])), candidate["chunk_id"])
            for candidate in source_candidates
        ),
        reverse=True,
    )
    best = [item for item in scored if item[0] >= PARTIAL_REVIEW_THRESHOLD][:3]
    return {
        "old_id": old_id,
        "status": PARTIAL if best else UNMAPPED,
        "targets": [chunk_id for _, chunk_id in best],
        "coverages": {chunk_id: round(score, 3) for score, chunk_id in best},
        "reason": (
            "old text spans a new chunk boundary"
            if best
            else "no new chunk overlaps the old text enough to suggest a mapping"
        ),
    }


def rekey_ids(
    old_ids: Sequence[str],
    old_chunks: Mapping[str, Mapping[str, Any]],
    new_chunks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Map a set of old ids and summarise how much of the work is automatic."""

    new_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for chunk in new_chunks:
        new_by_source.setdefault(chunk["source_id"], []).append(chunk)

    rows = [map_chunk_id(old_id, old_chunks, new_by_source) for old_id in dict.fromkeys(old_ids)]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "counts": dict(sorted(counts.items())),
        "needs_review": [row for row in rows if row["status"] in (PARTIAL, UNMAPPED)],
        "mapping": {
            row["old_id"]: row["targets"][0] for row in rows if row["status"] in (UNCHANGED, EXACT)
        },
        "rows": rows,
    }


def describe_review(rows: Sequence[Mapping[str, Any]]) -> str:
    """One-line summary used by the CLI and by tests."""

    if not rows:
        return "every id re-keyed automatically"
    worst = min(
        (min(row.get("coverages", {"-": 0.0}).values(), default=0.0) for row in rows),
        default=0.0,
    )
    return (
        f"{len(rows)} id(s) need review; best partial coverage seen {worst:.2f} "
        f"(threshold {PARTIAL_REVIEW_THRESHOLD})"
    )


def coverage_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Distribution of partial coverages, for reporting rather than gating."""

    values = [score for row in rows for score in row.get("coverages", {}).values()]
    if not values:
        return {}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }
