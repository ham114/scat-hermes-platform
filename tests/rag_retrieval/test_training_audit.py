"""Tests for the mechanical pre-screening of the M8 synthetic training records."""

from __future__ import annotations

from rag_retrieval.training_audit import (
    HIGH_RISK_POINTS,
    MEDIUM_RISK_POINTS,
    negative_signals,
    ngram_similarity,
    normalize_question,
    query_signals,
    source_balance,
    summarize_negative_risks,
    token_containment,
    token_jaccard,
)


def _chunk(
    chunk_id: str,
    source_id: str,
    ordinal: int,
    text: str,
    *,
    section_path: tuple[str, ...] = ("s",),
    exact_terms: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "ordinal": ordinal,
        "text": text,
        "section_path": list(section_path),
        "exact_terms": list(exact_terms),
    }


def _tokens(text: str) -> set[str]:
    from rag_retrieval.baseline import tokenize_unicode

    return set(tokenize_unicode(text))


# --- text helpers ----------------------------------------------------------------------


def test_normalize_question_collapses_case_width_and_whitespace() -> None:
    assert normalize_question("  What   IS  this？ ") == normalize_question("what is this?")


def test_ngram_similarity_is_one_for_identical_and_zero_for_disjoint_text() -> None:
    assert ngram_similarity("CGDF 的作用是什么", "CGDF 的作用是什么") == 1.0
    assert ngram_similarity("abc", "xyz") == 0.0


def test_containment_and_jaccard_measure_different_things() -> None:
    assert token_containment(["a", "b"], ["a", "b", "c", "d"]) == 1.0
    assert token_containment(["a", "z"], ["a", "b"]) == 0.5
    assert token_jaccard(["a", "b"], ["a", "b", "c", "d"]) == 0.5
    assert token_jaccard([], ["a"]) == 0.0


# --- negative screening ----------------------------------------------------------------


def test_adjacent_negative_is_flagged() -> None:
    positive = _chunk("p", "src", 10, "管制中心接管片段 centre_id 与开始时间")
    negative = _chunk("n", "src", 11, "完全无关的另一个主题的正文内容")

    row = negative_signals(
        negative_chunk=negative,
        positive_chunk=positive,
        query="centre_ctrl[] 记录什么？",
        negative_tokens=_tokens(negative["text"]),
        positive_tokens=_tokens(positive["text"]),
        structural_fill=False,
    )

    assert "adjacent-to-positive-chunk" in row["risk_reasons"]
    assert row["ordinal_distance"] == 1


def test_same_section_negative_is_flagged() -> None:
    positive = _chunk("p", "src", 10, "alpha beta gamma delta")
    negative = _chunk("n", "src", 40, "epsilon zeta eta theta")

    row = negative_signals(
        negative_chunk=negative,
        positive_chunk=positive,
        query="alpha 是什么？",
        negative_tokens=_tokens(negative["text"]),
        positive_tokens=_tokens(positive["text"]),
        structural_fill=False,
    )

    assert "same-section-as-positive" in row["risk_reasons"]
    assert row["ordinal_distance"] == 30


def test_negative_containing_a_queried_identifier_is_flagged() -> None:
    positive = _chunk("p", "src", 1, "unrelated baseline text")
    negative = _chunk(
        "n", "other", 7, "cmp_ground_speed 的单位换算说明", exact_terms=("cmp_ground_speed",)
    )

    row = negative_signals(
        negative_chunk=negative,
        positive_chunk=positive,
        query="cmp_ground_speed 的单位是什么？",
        negative_tokens=_tokens(negative["text"]),
        positive_tokens=_tokens(positive["text"]),
        structural_fill=False,
    )

    assert row["matched_query_identifiers"] == ["cmp_ground_speed"]
    assert "negative-contains-a-queried-identifier" in row["risk_reasons"]
    assert row["risk_points"] >= MEDIUM_RISK_POINTS


def test_structural_fill_alone_stays_below_the_medium_tier() -> None:
    positive = _chunk("p", "src", 1, "alpha beta gamma")
    negative = _chunk("n", "far", 900, "delta epsilon zeta")

    row = negative_signals(
        negative_chunk=negative,
        positive_chunk=positive,
        query="alpha 是什么？",
        negative_tokens=_tokens(negative["text"]),
        positive_tokens=_tokens(positive["text"]),
        structural_fill=True,
    )

    assert row["risk_tier"] == "low"
    assert row["risk_points"] < MEDIUM_RISK_POINTS


def test_a_clean_cross_source_negative_scores_zero() -> None:
    positive = _chunk("p", "src", 1, "alpha beta gamma")
    negative = _chunk("n", "far", 900, "delta epsilon zeta")

    row = negative_signals(
        negative_chunk=negative,
        positive_chunk=positive,
        query="alpha 是什么？",
        negative_tokens=_tokens(negative["text"]),
        positive_tokens=_tokens(positive["text"]),
        structural_fill=False,
    )

    assert row["risk_points"] == 0
    assert row["risk_tier"] == "none"
    assert row["risk_reasons"] == []


def test_risk_tiers_follow_the_documented_thresholds() -> None:
    positive = _chunk("p", "src", 1, "alpha beta gamma")
    # Identifier match (3) plus query coverage (2) must reach the high tier.
    negative = _chunk(
        "n", "far", 900, "cmp_ground_speed 的单位是什么", exact_terms=("cmp_ground_speed",)
    )

    row = negative_signals(
        negative_chunk=negative,
        positive_chunk=positive,
        query="cmp_ground_speed 的单位是什么？",
        negative_tokens=_tokens(negative["text"]),
        positive_tokens=_tokens(positive["text"]),
        structural_fill=False,
    )

    assert row["risk_points"] >= HIGH_RISK_POINTS
    assert row["risk_tier"] == "high"


def test_summarize_negative_risks_counts_tiers_and_reasons() -> None:
    rows = [
        {"risk_tier": "high", "risk_reasons": ["a"]},
        {"risk_tier": "medium", "risk_reasons": ["a", "b"]},
        {"risk_tier": "none", "risk_reasons": []},
    ]

    summary = summarize_negative_risks(rows)

    assert summary["pair_count"] == 3
    assert summary["high_risk_pairs"] == 1
    assert summary["medium_or_worse_pairs"] == 2
    assert summary["reason_counts"] == {"a": 2, "b": 1}


# --- query screening -------------------------------------------------------------------


def _record(question: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "query_id": "rt1_x",
        "split": "train",
        "language": "zh-CN",
        "source_id": "src",
        "template_id": "topic-definition-zh",
        "query": question,
    }
    record.update(overrides)
    return record


def test_english_template_query_with_mid_sentence_interrogative_is_not_flagged() -> None:
    """Regression: an anchored interrogative pattern silently missed every English template."""

    record = _record(
        "In the GBAR trajectory-prediction material, what is stated about the horizon query?",
        language="en",
    )

    row = query_signals(
        record=record, positive_chunk=_chunk("p", "src", 1, "text"), golden_questions=[]
    )

    assert "no-question-form-marker" not in row["quality_reasons"]
    assert row["quality_points"] == 0


def test_query_embedding_a_section_heading_is_flagged() -> None:
    record = _record("在固定翼无人机资料中，如何解释“4. 异常样本（ENU_, takeoff_at）” ？")

    row = query_signals(
        record=record, positive_chunk=_chunk("p", "src", 1, "text"), golden_questions=[]
    )

    assert "embeds-a-document-section-heading" in row["quality_reasons"]


def test_query_embedding_a_raw_field_list_is_flagged() -> None:
    record = _record("在项目文档中，如何解释“AnalysisResult 协议（analysis_result, LLM, UAV）”？")

    row = query_signals(
        record=record, positive_chunk=_chunk("p", "src", 1, "text"), golden_questions=[]
    )

    assert "embeds-a-raw-field-list" in row["quality_reasons"]


def test_query_without_any_question_form_is_flagged() -> None:
    record = _record("GBAR 部署参考文档第二章模型结构说明。")

    row = query_signals(
        record=record, positive_chunk=_chunk("p", "src", 1, "text"), golden_questions=[]
    )

    assert "no-question-form-marker" in row["quality_reasons"]


def test_query_near_a_golden_question_is_flagged() -> None:
    golden = ["为什么不能把 SCAT 的 predicted_trajectory[] 直接当成实际飞过的监视轨迹？"]
    record = _record("为什么不能把 SCAT 的 predicted_trajectory[] 直接当成实际飞过的监视轨迹？")

    row = query_signals(
        record=record, positive_chunk=_chunk("p", "src", 1, "text"), golden_questions=golden
    )

    assert "near-duplicate-of-a-golden-question" in row["quality_reasons"]
    assert row["max_golden_ngram_similarity"] == 1.0


# --- balance ---------------------------------------------------------------------------


def test_source_balance_reports_train_dominance() -> None:
    records = [
        {"source_id": "big", "language": "en", "split": "train"},
        {"source_id": "big", "language": "en", "split": "train"},
        {"source_id": "small", "language": "zh-CN", "split": "train"},
        {"source_id": "held", "language": "en", "split": "dev"},
    ]

    balance = source_balance(records)

    assert balance["record_count"] == 4
    assert balance["train_top_source"] == "big"
    assert balance["train_top_source_share"] == 2 / 3
    assert balance["by_split"] == {"train": 3, "dev": 1}
