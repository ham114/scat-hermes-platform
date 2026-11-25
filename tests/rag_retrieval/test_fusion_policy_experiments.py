import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"scripts"))
from fusion_policy_experiments import POLICIES, select
from rag_retrieval.baseline import Chunk, reciprocal_rank_fusion
from rag_retrieval.enhancements import apply_source_boost, infer_source_ids

PROFILE = {"rrf_k": 40, "exact_weight": 1., "source_multiplier": 2.2}


@pytest.fixture
def branches():
    dense = [f"d{i}" for i in range(200)]
    lexical = dense[:30]+[f"l{i}" for i in range(170)]
    exact = lexical[:10]+[f"e{i}" for i in range(190)]
    chunks = {cid: Chunk(cid, "source", "v7", (), cid) for cid in dense+lexical+exact}
    return dense, lexical, exact, chunks


@pytest.mark.parametrize("policy", POLICIES)
def test_all_policies_are_bounded_deduplicated_and_deterministic(policy, branches):
    args = (*branches, "query", PROFILE)
    first = select(policy, *args)
    assert first == select(policy, *args)
    assert len(first) == len(set(first)) == 100


def test_baseline_is_exact_original_formula(branches):
    dense, lexical, exact, chunks = branches
    fused = reciprocal_rank_fusion([lexical, dense, exact], weights=[1., 1., 1.],
                                   rank_constant=40, limit=200)
    expected = apply_source_boost(fused, chunks, infer_source_ids("query"), 2.2)
    assert select(POLICIES[0], *branches, "query", PROFILE) == [cid for cid, _ in expected[:100]]


def test_quota_overlaps_are_refilled_with_unique_evidence(branches):
    dense, lexical, exact, chunks = branches
    protected = select(POLICIES[2], *branches, "query", PROFILE)
    assert protected[:80] == dense[:80]
    quota = select(POLICIES[3], *branches, "query", PROFILE)
    assert quota[:70] == dense[:70]
    assert all(cid in lexical for cid in quota[70:90])
    assert all(cid in exact for cid in quota[90:100])


def test_missing_identifier_quota_uses_fusion_fill(branches):
    dense, lexical, _, chunks = branches
    pool = select(POLICIES[3], dense, lexical, [], chunks, "query", PROFILE)
    assert len(pool) == len(set(pool)) == 100


def test_small_corpus_and_invalid_policy():
    chunks = {"a": Chunk("a", "source", "v7", (), "a")}
    for policy in POLICIES:
        assert select(policy, ["a"], [], [], chunks, "query", PROFILE) == ["a"]
    with pytest.raises(ValueError, match="policy_not_predeclared"):
        select({"id": "tuned_after_results"}, ["a"], [], [], chunks, "query", PROFILE)
