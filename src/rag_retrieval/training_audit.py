"""Mechanical pre-screening for the M8 synthetic retrieval training records.

Every function here produces *review signals, not verdicts*. The output is evidence a
domain reviewer can check quickly; nothing in this module edits, drops, or relabels a
training record, and nothing here authorises training.

The screening exists because the A1.1 audit found at least nine probable false negatives
in only 24 reviewed dev questions, while the 563 training records were produced by the same
generator and have never been reviewed. Ranking the risk makes that review tractable.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rag_retrieval.baseline import tokenize_unicode
from rag_retrieval.enhancements import extract_identifiers

# Thresholds. Each turns a continuous signal into a discrete review trigger; they are
# deliberately permissive because a missed false negative costs a retrained model, while a
# false alarm only costs a reviewer a minute.
ADJACENT_ORDINAL_DISTANCE = 1
HIGH_QUERY_COVERAGE = 0.6
HIGH_POSITIVE_OVERLAP = 0.5
NEAR_DUPLICATE_QUESTION = 0.35

ADJACENT_RISK = 3
SAME_SECTION_RISK = 2
IDENTIFIER_RISK = 3
HIGH_QUERY_COVERAGE_RISK = 2
HIGH_POSITIVE_OVERLAP_RISK = 2
STRUCTURAL_FILL_RISK = 1
NEAR_DUPLICATE_QUERY_RISK = 3

HIGH_RISK_POINTS = 5
MEDIUM_RISK_POINTS = 3

# The interrogative may sit anywhere: the English templates read "In the GBAR material,
# what is stated about ...", so an anchored pattern silently misses every one of them.
_INTERROGATIVE = re.compile(
    r"什么|如何|为什么|为何|是否|哪些|哪个|多少|怎么|怎样|哪一|区别|作用|含义|影响|"
    r"\b(?:what|how|why|which|who|when|where|does|do|is|are|can|should|may|whether)\b",
    re.IGNORECASE,
)
_REFERENCE_MARKER = re.compile(r"…|\.\.\.|→|<-|->|\bsee\s+(table|figure|section)\b|参见")
# Template queries embed raw document structure: a numbered section heading, a parenthesised
# field dump, or a circled list marker. None of these belongs in a natural user question.
_SECTION_HEADING = re.compile(r"\d+[\.、]\s*[\u4e00-\u9fff]|[①②③④⑤⑥⑦⑧⑨⑩]")
_FIELD_DUMP = re.compile(
    r"[（(][A-Za-z_][A-Za-z0-9_]*\s*(?:,|，|/|\s)\s*[A-Za-z_][A-Za-z0-9_]*(?:\s*(?:,|，|/)\s*[A-Za-z_][A-Za-z0-9_]*)*[）)]"
)


def normalize_question(text: str) -> str:
    """Casefold and strip whitespace so two spellings of one question collapse."""

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def ngram_similarity(left: str, right: str, *, size: int = 3) -> float:
    """Character n-gram Jaccard over normalized text."""

    def grams(value: str) -> set[str]:
        normalized = normalize_question(value)
        if len(normalized) < size:
            return {normalized} if normalized else set()
        return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def token_containment(needle: Iterable[str], haystack: Iterable[str]) -> float:
    """Fraction of the needle's distinct tokens present in the haystack."""

    needle_set = set(needle)
    if not needle_set:
        return 0.0
    return len(needle_set & set(haystack)) / len(needle_set)


def token_jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """Token-set Jaccard similarity."""

    left_set, right_set = set(left), set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def chunk_text_tokens(chunk: Mapping[str, Any]) -> set[str]:
    """Tokens of a chunk's retrievable text, excluding metadata fields."""

    return set(tokenize_unicode(chunk["text"]))


def negative_signals(
    *,
    negative_chunk: Mapping[str, Any],
    positive_chunk: Mapping[str, Any],
    query: str,
    negative_tokens: set[str],
    positive_tokens: set[str],
    structural_fill: bool,
) -> dict[str, Any]:
    """Evidence for whether one negative could also answer the query."""

    same_source = negative_chunk["source_id"] == positive_chunk["source_id"]
    ordinal_distance = (
        abs(negative_chunk["ordinal"] - positive_chunk["ordinal"]) if same_source else None
    )
    adjacent = ordinal_distance is not None and ordinal_distance <= ADJACENT_ORDINAL_DISTANCE
    same_section = same_source and tuple(negative_chunk.get("section_path") or ()) == tuple(
        positive_chunk.get("section_path") or ()
    )

    query_tokens = tokenize_unicode(query)
    identifiers = extract_identifiers(query)
    haystack = negative_tokens | {
        str(term).casefold() for term in negative_chunk.get("exact_terms") or ()
    }
    matched_identifiers = sorted(
        identifier for identifier in identifiers if identifier.casefold() in haystack
    )
    query_coverage = token_containment(query_tokens, negative_tokens)
    positive_overlap = token_jaccard(positive_tokens, negative_tokens)

    points = 0
    reasons: list[str] = []
    if adjacent:
        points += ADJACENT_RISK
        reasons.append("adjacent-to-positive-chunk")
    if same_section:
        points += SAME_SECTION_RISK
        reasons.append("same-section-as-positive")
    if matched_identifiers:
        points += IDENTIFIER_RISK
        reasons.append("negative-contains-a-queried-identifier")
    if query_coverage >= HIGH_QUERY_COVERAGE:
        points += HIGH_QUERY_COVERAGE_RISK
        reasons.append("negative-covers-most-query-terms")
    if positive_overlap >= HIGH_POSITIVE_OVERLAP:
        points += HIGH_POSITIVE_OVERLAP_RISK
        reasons.append("high-token-overlap-with-positive")
    if structural_fill:
        points += STRUCTURAL_FILL_RISK
        reasons.append("structural-fill-not-retrieval-mined")

    if points >= HIGH_RISK_POINTS:
        tier = "high"
    elif points >= MEDIUM_RISK_POINTS:
        tier = "medium"
    elif points:
        tier = "low"
    else:
        tier = "none"

    return {
        "negative_chunk_id": negative_chunk["chunk_id"],
        "negative_source_id": negative_chunk["source_id"],
        "same_source_as_positive": same_source,
        "ordinal_distance": ordinal_distance,
        "same_section_as_positive": same_section,
        "matched_query_identifiers": matched_identifiers,
        "query_token_coverage": query_coverage,
        "positive_token_jaccard": positive_overlap,
        "risk_points": points,
        "risk_tier": tier,
        "risk_reasons": reasons,
    }


def query_signals(
    *,
    record: Mapping[str, Any],
    positive_chunk: Mapping[str, Any],
    golden_questions: Sequence[str],
) -> dict[str, Any]:
    """Evidence for whether a synthetic query is usable as a training query."""

    question = record["query"]
    similarity = max(
        (ngram_similarity(question, golden) for golden in golden_questions), default=0.0
    )
    reasons: list[str] = []
    points = 0
    if not _INTERROGATIVE.search(question):
        reasons.append("no-question-form-marker")
        points += 1
    if _REFERENCE_MARKER.search(question):
        reasons.append("contains-document-cross-reference")
        points += 1
    if _SECTION_HEADING.search(question):
        reasons.append("embeds-a-document-section-heading")
        points += 1
    if _FIELD_DUMP.search(question):
        reasons.append("embeds-a-raw-field-list")
        points += 1
    if similarity >= NEAR_DUPLICATE_QUESTION:
        reasons.append("near-duplicate-of-a-golden-question")
        points += NEAR_DUPLICATE_QUERY_RISK
    return {
        "query_id": record["query_id"],
        "split": record["split"],
        "language": record["language"],
        "source_id": record["source_id"],
        "template_id": record["template_id"],
        "query": question,
        "query_chars": len(question),
        "max_golden_ngram_similarity": similarity,
        "positive_source_id": positive_chunk["source_id"],
        "quality_points": points,
        "quality_reasons": reasons,
    }


def summarize_negative_risks(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-negative rows into tier and reason counts."""

    tiers: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        tiers[row["risk_tier"]] = tiers.get(row["risk_tier"], 0) + 1
        for reason in row["risk_reasons"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "pair_count": len(rows),
        "tier_counts": dict(sorted(tiers.items(), key=lambda item: -item[1])),
        "reason_counts": dict(sorted(reasons.items(), key=lambda item: -item[1])),
        "high_risk_pairs": tiers.get("high", 0),
        "medium_or_worse_pairs": tiers.get("high", 0) + tiers.get("medium", 0),
    }


def source_balance(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Share of each source and language so gradient dominance is visible."""

    total = len(records)
    train = [record for record in records if record["split"] == "train"]
    train_by_source = tally_from(train, "source_id")
    train_total = len(train) or 1
    return {
        "record_count": total,
        "by_source": tally_from(records, "source_id"),
        "by_language": tally_from(records, "language"),
        "by_split": tally_from(records, "split"),
        "train_by_source": train_by_source,
        "train_top_source_share": (
            max(train_by_source.values()) / train_total if train_by_source else 0.0
        ),
        "train_top_source": (
            max(train_by_source, key=lambda key: train_by_source[key]) if train_by_source else None
        ),
        "sources_without_records_note": (
            "The dataset manifest lists four active sources that produced no records because "
            "every candidate positive was shielded, too short, or reference-like. This is a "
            "coverage limit, not a file omission, and must not be filled by relaxing the shield."
        ),
    }


def tally_from(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record[field])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))
