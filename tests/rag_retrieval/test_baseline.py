from __future__ import annotations

import math

import pytest

from rag_retrieval.baseline import (
    BM25Index,
    Chunk,
    coverage_at_depths,
    evaluate_ranking,
    normalize_scores_minmax,
    reciprocal_rank_fusion,
    tokenize_unicode,
)


def make_chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_id="source",
        source_version="v1",
        section_path=(),
        text=text,
    )


def test_unicode_tokenizer_preserves_identifiers_and_adds_chinese_bigrams() -> None:
    tokens = tokenize_unicode("I062/136 measured_flight_level 是飞行高度层")

    assert "i062/136" in tokens
    assert "measured_flight_level" in tokens
    assert "飞行" in tokens
    assert "高度" in tokens


def test_bm25_ranks_matching_chunk_first_and_breaks_ties_by_id() -> None:
    index = BM25Index(
        [
            make_chunk("b", "监视航迹使用 ASTERIX CAT062"),
            make_chunk("a", "CAT062 表示系统航迹"),
            make_chunk("c", "固定翼无人机惯导"),
        ]
    )

    ranked = index.rank("CAT062 系统航迹", limit=3)

    assert ranked[0][0] == "a"
    assert {chunk_id for chunk_id, _ in ranked[:2]} == {"a", "b"}


def test_rrf_deduplicates_each_ranking_and_is_deterministic() -> None:
    fused = reciprocal_rank_fusion([["a", "a", "b"], ["b", "c"]], rank_constant=60)

    assert [chunk_id for chunk_id, _ in fused] == ["b", "a", "c"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_rrf_supports_positive_source_weights() -> None:
    fused = reciprocal_rank_fusion(
        [["lexical", "shared"], ["dense", "shared"]],
        rank_constant=20,
        weights=[2.0, 1.0],
    )

    assert [chunk_id for chunk_id, _ in fused] == ["shared", "lexical", "dense"]
    with pytest.raises(ValueError, match="weights"):
        reciprocal_rank_fusion([["a"]], weights=[1.0, 2.0])


def test_metrics_use_binary_relevance_and_macro_recall() -> None:
    metrics = evaluate_ranking({"b", "d"}, ["a", "b", "c", "d"])

    assert metrics.recall_at_5 == 1.0
    assert metrics.hit_at_5 == 1.0
    assert metrics.reciprocal_rank == 0.5
    expected_dcg = 1 / math.log2(3) + 1 / math.log2(5)
    expected_idcg = 1 + 1 / math.log2(3)
    assert metrics.ndcg_at_10 == pytest.approx(expected_dcg / expected_idcg)


def test_candidate_coverage_supports_arbitrary_depths() -> None:
    metrics = coverage_at_depths({"b", "d"}, ["a", "b", "c", "d"], [3, 1, 3])

    assert metrics == {
        "recall_at_1": 0.0,
        "hit_at_1": 0.0,
        "recall_at_3": 0.5,
        "hit_at_3": 1.0,
    }


def test_minmax_normalization_preserves_order_and_equal_ties() -> None:
    assert normalize_scores_minmax([2.0, 4.0, 3.0]) == [0.0, 1.0, 0.5]
    assert normalize_scores_minmax([2.0, 2.0]) == [1.0, 1.0]


def test_invalid_bm25_and_empty_relevance_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BM25Index([])
    with pytest.raises(ValueError, match="relevant"):
        evaluate_ranking(set(), [])
