"""Adapt reviewed development candidates to the existing claim-group scorer."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

DATASET_VERSION = "ragas-user-reviewed-dev40-v1"
CLAIM_VERSION = DATASET_VERSION + "-claim-groups-v1"


def minimal_bundles(units: list[set[str]]) -> list[list[str]]:
    """Every unit is required; each unit may have several interchangeable chunks."""
    if not units or any(not unit for unit in units):
        raise ValueError("empty_support_unit")
    frontier = {frozenset()}
    for unit in units:
        expanded = {bundle | {chunk_id} for bundle in frontier for chunk_id in unit}
        frontier = {bundle for bundle in expanded if not any(x < bundle for x in expanded)}
        if len(frontier) > 128:
            raise ValueError("support_alternative_limit")
    return [sorted(bundle) for bundle in sorted(frontier, key=lambda x: (len(x), sorted(x)))]


def check_reference(ref: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
    row = records.get(ref.get("chunk_id"))
    if row is None:
        raise ValueError("unknown_evidence_chunk")
    for field in ("source_id", "source_version", "text_sha256"):
        if ref.get(field) != row.get(field):
            raise ValueError("evidence_identity_mismatch")
    quote = ref.get("quote")
    start, end = ref.get("start_char"), ref.get("end_char")
    if (
        not isinstance(quote, str)
        or not quote.strip()
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or row["text"][start:end] != quote
    ):
        raise ValueError("evidence_quote_or_offset_invalid")


def scoped_support(ref: dict[str, Any], records: dict[str, dict[str, Any]]) -> set[str]:
    """Identical words alone do not disambiguate different tables or datasets."""
    original = records[ref["chunk_id"]]
    return {
        cid
        for cid, row in records.items()
        if row["source_id"] == original["source_id"]
        and row["source_version"] == original["source_version"]
        and row.get("section_path", []) == original.get("section_path", [])
        and ref["quote"] in row["text"]
    }


def convert_reviewed(
    candidates: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    rules: dict[str, Any],
    corpus_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    records = {row["chunk_id"]: row for row in corpus}
    if len(records) != len(corpus):
        raise ValueError("duplicate_corpus_chunk")
    for row in corpus:
        if hashlib.sha256(row["text"].encode()).hexdigest() != row["text_sha256"]:
            raise ValueError("corpus_text_hash_invalid")
    if len({c["qid"] for c in candidates}) != len(candidates) or not candidates:
        raise ValueError("duplicate_or_empty_cases")
    rule_map = rules.get("cases", {})
    if not set(rule_map).issubset({c["qid"] for c in candidates}):
        raise ValueError("rule_case_missing")
    cases, labels, audit_cases = [], [], []
    for candidate in sorted(candidates, key=lambda c: c["qid"]):
        if candidate["corpus_sha256"] != corpus_sha256 or candidate["split"] != "development":
            raise ValueError("candidate_scope_or_corpus_mismatch")
        if candidate["human_review"] not in ("approved", "revised_after_user_review"):
            raise ValueError("candidate_not_user_reviewed")
        if candidate["human_review"] == "revised_after_user_review" and not rules.get(
            "authorize_use_of_presented_revision"
        ):
            raise ValueError("revision_use_not_authorized")
        cid = candidate["qid"]
        rule = rule_map.get(cid, {})
        groups, excluded = [], []
        valid_indices = {str(i + 1) for i in range(len(candidate["claims"]))}
        for key in ("exclude_claims", "claim_modes", "extra_alternatives"):
            if not set(rule.get(key, {})).issubset(valid_indices):
                raise ValueError("rule_claim_missing")
        for number, claim in enumerate(candidate["claims"], 1):
            key = str(number)
            refs = claim["evidence"]
            if not claim["text"].strip() or not refs:
                raise ValueError("empty_claim")
            for ref in refs:
                check_reference(ref, records)
            if key in rule.get("exclude_claims", {}):
                excluded.append(
                    {
                        "claim_number": number,
                        "claim": claim["text"],
                        "reason": rule["exclude_claims"][key],
                    }
                )
                continue
            mode = rule.get("claim_modes", {}).get(key, "all-witnesses")
            units = [scoped_support(ref, records) for ref in refs]
            if mode == "any-witness":
                alternatives = [[chunk] for chunk in sorted(set.union(*units))]
            elif mode == "all-witnesses":
                alternatives = minimal_bundles(units)
            else:
                raise ValueError("unknown_claim_mode")
            reviewed_extra = rule.get("extra_alternatives", {}).get(key, [])
            for extra in reviewed_extra:
                ids = extra["chunk_ids"]
                if not ids or not extra.get("rationale") or set(ids) - set(records):
                    raise ValueError("invalid_reviewed_alternative")
                if not extra.get("quotes") or any(
                    q["chunk_id"] not in ids
                    or not q["quote"]
                    or q["quote"] not in records[q["chunk_id"]]["text"]
                    for q in extra["quotes"]
                ):
                    raise ValueError("reviewed_alternative_quote_invalid")
                alternatives.append(sorted(set(ids)))
            bundles = {frozenset(a) for a in alternatives}
            minimal = {b for b in bundles if not any(a < b for a in bundles)}
            alternatives = [sorted(b) for b in sorted(minimal, key=lambda x: (len(x), sorted(x)))]
            groups.append(
                {
                    "group_id": f"claim-{number:02d}",
                    "claim": claim["text"],
                    "mode": "any-alternative",
                    "alternatives": alternatives,
                    "audit": {
                        "witness_policy": mode,
                        "original_evidence": refs,
                        "reviewed_extra_alternatives": reviewed_extra,
                    },
                }
            )
        if not groups:
            raise ValueError("no_document_claim_groups")
        required = sorted({chunk for g in groups for a in g["alternatives"] for chunk in a})
        singleton_complete = set.intersection(
            *[{a[0] for a in g["alternatives"] if len(a) == 1} for g in groups]
        )
        source_sets = [
            {records[x]["source_id"] for a in g["alternatives"] for x in a} for g in groups
        ]
        possible_single_sources = set.intersection(*source_sets)
        cases.append(
            {
                "case_id": cid,
                "split": "dev",
                "expected_route": "rag",
                "language": "zh-CN",
                "category": candidate["type"],
                "question": candidate["question"],
                "reference": candidate["reference"],
                "required_claims": [g["claim"] for g in groups],
                "required_chunk_ids": required,
                "deterministic_requirements": [],
                "authored_difficulty": candidate["difficulty"],
                "human_review": candidate["human_review"],
            }
        )
        labels.append({"case_id": cid, "document_claim_groups": groups})
        audit_cases.append(
            {
                "case_id": cid,
                "input_claim_count": len(candidate["claims"]),
                "scored_claim_count": len(groups),
                "excluded": excluded,
                "single_chunk_can_cover_all": sorted(singleton_complete),
                "possible_single_source_coverage": sorted(possible_single_sources),
                "authored_category": candidate["type"],
                "authored_multi_source_but_single_chunk_sufficient": candidate["type"]
                == "multi_source"
                and bool(singleton_complete),
            }
        )
    common = {
        "scope": "user-reviewed-development-not-independent-admission",
        "corpus_sha256": corpus_sha256,
        "external_llm_calls": 0,
    }
    return (
        {**common, "dataset_version": DATASET_VERSION, "cases": cases},
        {**common, "dataset_version": CLAIM_VERSION, "cases": labels},
        {
            **common,
            "case_count": len(cases),
            "groups": sum(len(c["document_claim_groups"]) for c in labels),
            "by_category": dict(Counter(c["category"] for c in cases)),
            "cases": audit_cases,
            "excluded_claims": sum(len(c["excluded"]) for c in audit_cases),
            "labels_frozen_before_rankings": True,
            "alternative_coverage_exhaustive": False,
        },
    )
