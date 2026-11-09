"""Claim-group scoring, mechanical grouping, and stratification for M8 evaluation."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from rag_retrieval.enhancements import extract_identifiers

ANY_SUPPORTING = "any-supporting"
UNREVIEWED_CARRYOVER = "unreviewed-carryover"

# Text of the owner-confirmed automatic rules. The wording is copied verbatim from
# knowledge_base/training/m8-a1.1-error-audit-v1/claim_group_rules.v1.json so that a
# mechanical application can be traced back to the frozen rule it implements.
AUTOMATIC_RULES: dict[str, str] = {
    ANY_SUPPORTING: (
        "One document claim group; every original required chunk and every "
        "agent-reviewed correct candidate is a singleton alternative."
    ),
    UNREVIEWED_CARRYOVER: (
        "One document claim group; every original required chunk is a singleton alternative."
    ),
}

# Structural risk signals for a case whose claim structure was assigned mechanically
# instead of by review. The thresholds are chosen so that every case the A1.1 audit
# treated manually would be flagged; see assess_mechanical_fidelity() for the check.
MULTI_TARGET_RISK = 2
OVERSIZED_TARGET_RISK = 2
DEEP_EVIDENCE_RISK = 1
NON_DOCUMENT_ROUTE_RISK = 2
OVERSIZED_TARGET_CHUNKS = 3
DEEP_EVIDENCE_CHUNKS = 5


def deduplicate_alternatives(alternatives: Sequence[Sequence[str]]) -> list[list[str]]:
    """Drop repeated chunk IDs inside an alternative and repeated alternatives."""

    result: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for alternative in alternatives:
        normalized = tuple(dict.fromkeys(alternative))
        if not normalized:
            raise ValueError("Evidence alternatives cannot be empty")
        if normalized not in seen:
            result.append(list(normalized))
            seen.add(normalized)
    return result


def mechanical_groups(
    case: Mapping[str, Any], correct_chunk_ids: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Apply the frozen automatic rule: one group, every accepted chunk a singleton."""

    accepted = list(dict.fromkeys([*case["required_chunk_ids"], *correct_chunk_ids]))
    if not accepted:
        raise ValueError(f"Case has no required evidence: {case['case_id']}")
    claims = case.get("required_claims") or [""]
    return [
        {
            "group_id": "answer-support",
            "claim": claims[0],
            "mode": "any-alternative",
            "alternatives": deduplicate_alternatives([[chunk_id] for chunk_id in accepted]),
        }
    ]


def group_completion_rank(group: Mapping[str, Any], ranked_ids: Sequence[str]) -> int | None:
    """Lowest rank at which one whole alternative is retrieved, else None."""

    positions = {chunk_id: index for index, chunk_id in enumerate(ranked_ids, start=1)}
    ranks: list[int] = []
    for alternative in group["alternatives"]:
        if all(chunk_id in positions for chunk_id in alternative):
            ranks.append(max(positions[chunk_id] for chunk_id in alternative))
    return min(ranks) if ranks else None


def score_case(case: Mapping[str, Any], ranked_ids: Sequence[str]) -> dict[str, Any]:
    """Score one case under the contract: every claim group must be covered."""

    group_rows = []
    for group in case["document_claim_groups"]:
        rank = group_completion_rank(group, ranked_ids)
        group_rows.append(
            {
                "group_id": group["group_id"],
                "completion_rank": rank,
                "covered_at_5": rank is not None and rank <= 5,
                "covered_at_10": rank is not None and rank <= 10,
            }
        )
    ranks = [row["completion_rank"] for row in group_rows if row["completion_rank"]]
    group_count = len(group_rows)
    if group_count == 0:
        return {
            "case_id": case["case_id"],
            "document_group_count": 0,
            "document_group_recall_at_5": 0.0,
            "document_group_recall_at_10": 0.0,
            "document_any_at_5": 0.0,
            "document_any_at_10": 0.0,
            "document_complete_at_5": 0.0,
            "document_complete_at_10": 0.0,
            "reciprocal_rank": 0.0,
            "mean_group_discount_at_10": 0.0,
            "deterministic_requirement_count": len(case.get("deterministic_requirements", [])),
            "document_groups": [],
        }
    recall_5 = sum(row["covered_at_5"] for row in group_rows) / group_count
    recall_10 = sum(row["covered_at_10"] for row in group_rows) / group_count
    first_rank = min(ranks) if ranks else None
    mean_discount_10 = statistics.fmean(
        1 / math.log2(row["completion_rank"] + 1)
        if row["completion_rank"] is not None and row["completion_rank"] <= 10
        else 0.0
        for row in group_rows
    )
    return {
        "case_id": case["case_id"],
        "document_group_count": group_count,
        "document_group_recall_at_5": recall_5,
        "document_group_recall_at_10": recall_10,
        "document_any_at_5": float(recall_5 > 0),
        "document_any_at_10": float(recall_10 > 0),
        "document_complete_at_5": float(recall_5 == 1.0),
        "document_complete_at_10": float(recall_10 == 1.0),
        "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
        "mean_group_discount_at_10": mean_discount_10,
        "deterministic_requirement_count": len(case.get("deterministic_requirements", [])),
        "document_groups": group_rows,
    }


def aggregate_case_scores(scores: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Macro-average the per-case claim-group metrics."""

    keys = (
        "document_group_recall_at_5",
        "document_group_recall_at_10",
        "document_any_at_5",
        "document_any_at_10",
        "document_complete_at_5",
        "document_complete_at_10",
        "reciprocal_rank",
        "mean_group_discount_at_10",
    )
    return {key: statistics.fmean(score[key] for score in scores) for key in keys}


def accepted_evidence_ids(case: Mapping[str, Any]) -> set[str]:
    """Every chunk that would count as complete evidence for at least one group."""

    return {
        chunk_id
        for group in case["document_claim_groups"]
        for alternative in group["alternatives"]
        for chunk_id in alternative
    }


def recall_at_k_ceiling(group_counts: Sequence[int], k: int) -> float:
    """Highest Recall@k a perfect ranker could reach for the given group counts."""

    if not group_counts:
        raise ValueError("At least one case is required")
    return statistics.fmean(min(count, k) / count for count in group_counts)


def evidence_breadth_bucket(required_chunk_count: int) -> str:
    if required_chunk_count <= 1:
        return "single"
    if required_chunk_count <= 4:
        return "few-2-4"
    return "many-5-plus"


def identifier_density_bucket(identifier_count: int) -> str:
    if identifier_count == 0:
        return "none"
    if identifier_count <= 2:
        return "low-1-2"
    return "high-3-plus"


def stratify_case(case: Mapping[str, Any], chunk_sources: Mapping[str, str]) -> dict[str, Any]:
    """Derive computable, auditable difficulty dimensions.

    The frozen `difficulty` label is left untouched: it carries no `hard` cases and
    re-labelling it would fabricate a judgement. These dimensions are reproducible
    from the artifact itself, so `hard` subset claims can be re-derived at any time.
    """

    chunk_ids = list(case["required_chunk_ids"])
    sources = {chunk_sources.get(chunk_id) for chunk_id in chunk_ids}
    sources.discard(None)
    identifiers = extract_identifiers(case["question"])
    return {
        "case_id": case["case_id"],
        "split": case["split"],
        "category": case["category"],
        "language": case["language"],
        "expected_route": case["expected_route"],
        "frozen_difficulty": case.get("difficulty"),
        "required_chunk_count": len(chunk_ids),
        "evidence_target_count": len(case["evidence_targets"]),
        "distinct_required_source_count": len(sources),
        "identifier_count": len(identifiers),
        "identifiers": list(identifiers),
        "evidence_breadth": evidence_breadth_bucket(len(chunk_ids)),
        "identifier_density": identifier_density_bucket(len(identifiers)),
        "cross_source": len(sources) > 1,
        "has_deterministic_requirement": bool(case.get("deterministic_requirement")),
        "must_refuse": bool(case.get("must_refuse")),
    }


def mechanical_review_risk(case: Mapping[str, Any]) -> tuple[int, list[str]]:
    """Score how likely a mechanical claim structure is wrong for one case."""

    targets = case["evidence_targets"]
    target_chunks = [len(target["required_chunk_ids"]) for target in targets]
    total_chunks = len(case["required_chunk_ids"])
    points = 0
    reasons: list[str] = []
    if len(targets) > 1:
        points += MULTI_TARGET_RISK
        reasons.append("multi-evidence-target-redundancy-risk")
    if target_chunks and max(target_chunks) > OVERSIZED_TARGET_CHUNKS:
        points += OVERSIZED_TARGET_RISK
        reasons.append("locator-over-expansion-risk")
    if total_chunks > DEEP_EVIDENCE_CHUNKS:
        points += DEEP_EVIDENCE_RISK
        reasons.append("ceiling-relevant-evidence-depth")
    if case["expected_route"] != "rag":
        points += NON_DOCUMENT_ROUTE_RISK
        reasons.append("non-document-route")
    return points, reasons


def compare_claim_structures(
    mechanical: Mapping[str, Any], reference: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare a mechanical grouping against a reviewed grouping for the same case."""

    mechanical_ids = accepted_evidence_ids(mechanical)
    reference_ids = accepted_evidence_ids(reference)
    union = mechanical_ids | reference_ids
    intersection = mechanical_ids & reference_ids
    return {
        "case_id": mechanical["case_id"],
        "mechanical_group_count": len(mechanical["document_claim_groups"]),
        "reference_group_count": len(reference["document_claim_groups"]),
        "group_count_match": (
            len(mechanical["document_claim_groups"]) == len(reference["document_claim_groups"])
        ),
        "accepted_set_match": mechanical_ids == reference_ids,
        "accepted_set_jaccard": len(intersection) / len(union) if union else 1.0,
        "mechanical_only_ids": sorted(mechanical_ids - reference_ids),
        "reference_only_ids": sorted(reference_ids - mechanical_ids),
    }
