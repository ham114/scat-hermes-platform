"""Shared, bounded candidate construction for runtime and offline evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rag_retrieval.baseline import Chunk, reciprocal_rank_fusion
from rag_retrieval.enhancements import (
    apply_source_boost,
    infer_source_ids,
    rank_exact_identifier_matches,
)

LEGACY_POLICY = "rrf200_source_top100"
RETAIN_DENSE_POLICY = "dense100_rrf50"
WEIGHTED_POLICY = "rrf200_dense2_source_top100"


@dataclass(frozen=True)
class CandidateSelection:
    ids: tuple[str, ...]
    protected_ids: tuple[str, ...]
    fused_ids: tuple[str, ...]
    boosted_ids: tuple[str, ...]
    signals: dict[str, dict]
    scores: dict[str, float]


def select_candidates(
    *,
    query: str,
    dense: Sequence[str],
    lexical: Sequence[str],
    exact: Sequence[str],
    chunks_by_id: Mapping[str, Chunk],
    profile: Mapping,
    policy: str = LEGACY_POLICY,
    limit: int = 100,
) -> CandidateSelection:
    """Retain dense100; fill at most 50 unique extras from the full RRF union.

    Source boost may order the extra candidates, but cannot evict a protected
    dense candidate. No relevance labels or dataset-specific rules are used.
    """
    depth = int(profile["candidate_k"])
    if not query.strip() or type(limit) is not int:
        raise ValueError("invalid_candidate_query_or_limit")
    if policy in {LEGACY_POLICY, WEIGHTED_POLICY}:
        if not 1 <= limit <= depth:
            raise ValueError("invalid_candidate_query_or_limit")
        if policy == WEIGHTED_POLICY and (limit != 100 or depth != 200):
            raise ValueError("weighted2_requires_leaf200_limit100")
    elif policy == RETAIN_DENSE_POLICY:
        if limit != 150 or depth < 100:
            raise ValueError("dense100_rrf50_requires_limit150")
    else:
        raise ValueError("unknown_candidate_policy")
    branches = [list(lexical[:depth]), list(dense[:depth]), list(exact[:depth])]
    for branch in branches:
        if len(branch) != len(set(branch)) or any(cid not in chunks_by_id for cid in branch):
            raise ValueError("invalid_candidate_branch")
    rankings, weights = branches[:2], [1.0, 2.0 if policy == WEIGHTED_POLICY else 1.0]
    if branches[2]:
        rankings.append(branches[2])
        weights.append(float(profile["exact_weight"]))
    fused = reciprocal_rank_fusion(
        rankings,
        rank_constant=int(profile["rrf_k"]),
        weights=weights,
        limit=None if policy == RETAIN_DENSE_POLICY else depth,
    )
    boosted = apply_source_boost(
        fused, chunks_by_id, infer_source_ids(query), float(profile["source_multiplier"])
    )
    protected = tuple(branches[1][:100]) if policy == RETAIN_DENSE_POLICY else ()
    capacity = min(limit, len(protected) + 50) if policy == RETAIN_DENSE_POLICY else limit
    selected = list(protected)
    seen = set(selected)
    for cid, _ in boosted:
        if len(selected) >= capacity:
            break
        if cid not in seen:
            selected.append(cid)
            seen.add(cid)
    maps = {
        name: {cid: i + 1 for i, cid in enumerate(ids)}
        for name, ids in [
            ("bm25_rank", branches[0]),
            ("dense_rank", branches[1]),
            ("exact_rank", branches[2]),
            ("fusion_rank", [cid for cid, _ in fused]),
            ("source_boost_rank", [cid for cid, _ in boosted]),
        ]
    }
    protected_set = set(protected)
    signals = {
        cid: {
            **{name: ranks.get(cid) for name, ranks in maps.items()},
            "candidate_rank": i + 1,
            "protected_dense": cid in protected_set,
            "selected_by": "dense_reserve" if cid in protected_set else "fusion",
        }
        for i, cid in enumerate(selected)
    }
    return CandidateSelection(
        tuple(selected), protected, tuple(cid for cid, _ in fused),
        tuple(cid for cid, _ in boosted), signals, dict(boosted),
    )


def collect_rankings(index, query: str, depth: int) -> tuple[list, list, list]:
    """Encode once using the same whitespace normalization as the runtime."""
    import numpy as np

    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must not be empty")
    lexical = [cid for cid, _ in index.bm25.rank(normalized, limit=depth)]
    vector = np.asarray(
        index.model.encode([normalized], normalize_embeddings=True, show_progress_bar=False)[0],
        dtype=np.float32,
    )
    dense = [
        index.chunk_ids[i]
        for i in np.argsort(-(index.corpus_vectors @ vector), kind="stable")[:depth]
    ]
    exact = [
        cid for cid, _ in rank_exact_identifier_matches(normalized, index.chunks, limit=depth)
    ]
    return dense, lexical, exact


def build_candidates(index, query: str, profile: Mapping, *, policy=LEGACY_POLICY, limit=100):
    normalized = " ".join(query.split())
    dense, lexical, exact = collect_rankings(index, normalized, int(profile["candidate_k"]))
    return select_candidates(
        query=normalized, dense=dense, lexical=lexical, exact=exact,
        chunks_by_id=index.chunks_by_id, profile=profile, policy=policy, limit=limit,
    )
