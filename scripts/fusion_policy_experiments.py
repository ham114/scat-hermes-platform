"""Predeclared fusion-only policies; independent of relevance annotations."""
from rag_retrieval.baseline import reciprocal_rank_fusion
from rag_retrieval.enhancements import apply_source_boost, infer_source_ids

POLICIES = [
    {"id": "baseline", "label": "原生产融合100", "dense_weight": 1., "mode": "rrf200"},
    {"id": "weighted2", "label": "BGE权重2的RRF100", "dense_weight": 2., "mode": "rrf200"},
    {"id": "dense80", "label": "保留BGE80＋融合20", "dense_weight": 1., "mode": "reserve80"},
    {"id": "quota70", "label": "BGE70＋BM2520＋标识符10", "dense_weight": 1., "mode": "quota70"},
    {"id": "dense100", "label": "纯BGE100对照", "dense_weight": 1., "mode": "dense100"},
]


def select(policy, dense, lexical, exact, chunks, query, profile):
    """All pools <=100, same leaf200, RRF k40 and source multiplier2.2.

    Baseline and weighted retain the original intermediate cut200. Quota
    policies use the full union; source boost cannot evict reserved candidates.
    Branch overlaps are skipped and the requested unique quota is refilled.
    """
    if policy not in POLICIES:
        raise ValueError("policy_not_predeclared")
    for branch in [dense, lexical, exact]:
        if len(branch) != len(set(branch)) or any(cid not in chunks for cid in branch):
            raise ValueError("invalid_branch")
    dense, lexical, exact = dense[:200], lexical[:200], exact[:200]
    if policy["mode"] == "dense100":
        return list(dense[:100])
    rankings = [lexical, dense]
    weights = [1., policy["dense_weight"]]
    if exact:
        rankings.append(exact)
        weights.append(float(profile["exact_weight"]))
    fused = reciprocal_rank_fusion(rankings, weights=weights,
                                   rank_constant=int(profile["rrf_k"]),
                                   limit=200 if policy["mode"] == "rrf200" else None)
    boosted = apply_source_boost(fused, chunks, infer_source_ids(query),
                                 float(profile["source_multiplier"]))
    pool, seen = [], set()

    def add(ids, quota):
        count = 0
        for cid in ids:
            if count >= quota or len(pool) >= 100:
                break
            if cid not in seen:
                pool.append(cid)
                seen.add(cid)
                count += 1

    if policy["mode"] == "reserve80":
        add(dense, 80)
    elif policy["mode"] == "quota70":
        add(dense, 70)
        add(lexical, 20)
        add(exact, 10)
    add([cid for cid, _ in boosted], 100-len(pool))
    return pool
