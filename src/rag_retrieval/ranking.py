"""Pinned production rank fusion; relevance scores retain their original meaning."""

from rag_retrieval.rerank_comparison import order_by_scores

RAW_RANKING = "reranker"
RRF_RANKING = "rank_rrf_4_1_k20"


def rank_candidates(ids, scores, policy=RAW_RANKING):
    base = order_by_scores(ids, scores)
    if policy == RAW_RANKING:
        return base
    if policy != RRF_RANKING:
        raise ValueError("unapproved_ranking_policy")
    positions = {cid: i + 1 for i, cid in enumerate(ids)}
    ranks = {cid: i + 1 for i, cid in enumerate(base)}
    return sorted(ids, key=lambda cid: (
        -(4.0 / (20 + ranks[cid]) + 1.0 / (20 + positions[cid])),
        ranks[cid], positions[cid],
    ))
