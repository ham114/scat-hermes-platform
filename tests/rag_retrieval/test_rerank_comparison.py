from __future__ import annotations

import numpy as np
import pytest

from rag_retrieval.baseline import Chunk, tokenize_unicode
from rag_retrieval.rerank_comparison import (
    frozen_candidates,
    order_by_scores,
    pack_ids,
    ranking_metrics,
)
from rag_retrieval.runtime import M8RRFIndex


class FixedEncoder:
    def encode(self, sentences, **kwargs):
        return np.array([[1.0, 0.0]] * len(sentences), dtype=np.float32)


@pytest.mark.parametrize("query", ["GBAR feature_order", "SCAT", "UAV I021/090", " \nGBAR   输入 "])
def test_candidate_prefix_matches_unmodified_runtime(query):
    chunks = [
        Chunk(
            f"c{i}",
            ["gbar-method-curated", "scat-dataset-curated", "uav-research-guide"][i % 3],
            "v1",
            ("input",),
            f"GBAR SCAT UAV document {i}",
            ("feature_order",),
        )
        for i in range(24)
    ]
    index = M8RRFIndex(
        chunks=chunks,
        records={
            c.chunk_id: {
                "source_id": c.source_id,
                "source_version": c.source_version,
                "text": c.text,
            }
            for c in chunks
        },
        model=FixedEncoder(),
        corpus_vectors=np.array([[1.0, 0.0]] * 24),
        tokenizer=tokenize_unicode,
    )
    ids = frozen_candidates(index, query)
    assert ids[:10] == [r["chunk_id"] for r in index.search(query, limit=10)]
    assert len(ids) == len(set(ids)) == 24


@pytest.mark.parametrize(
    "ids,scores",
    [(["a"], []), (["a", "a"], [0.1, 0.2]), (["a"], [float("nan")]), (["a"], [float("inf")])],
)
def test_rejects_invalid_or_incomplete_scores(ids, scores):
    with pytest.raises(ValueError, match="invalid_rerank_scores"):
        order_by_scores(ids, scores)


def test_stable_ties_preserve_the_original_candidate_order():
    assert order_by_scores(["a", "b", "c"], [0.2, 0.9, 0.9]) == ["b", "c", "a"]


def test_truncation_retains_complete_instruction_and_answer_suffix():
    assert pack_ids(list(range(20)), [90, 91], [98, 99], 8) == [90, 91, 0, 1, 2, 3, 98, 99]
    with pytest.raises(ValueError, match="token_budget"):
        pack_ids([1], [90, 91], [98, 99], 4)


def test_multi_chunk_alternatives_require_the_entire_bundle():
    case = {
        "case_id": "synthetic",
        "document_claim_groups": [
            {"group_id": "a", "alternatives": [["a", "b"], ["c"]]},
            {"group_id": "d", "alternatives": [["d"]]},
        ],
    }
    partial = ranking_metrics(case, ["a", "d"])
    complete = ranking_metrics(case, ["a", "b", "d"])
    alternative = ranking_metrics(case, ["c", "d"])
    assert partial["group_recall_at_5"] == 0.5
    assert partial["complete_at_5"] == 0
    assert complete["complete_at_5"] == alternative["complete_at_5"] == 1
