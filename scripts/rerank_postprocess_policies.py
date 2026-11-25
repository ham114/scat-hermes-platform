"""Label-blind, deterministic postprocessing of existing reranker scores."""
from __future__ import annotations

import math
import re
from collections import Counter

POLICIES = [
    {"id": "reranker", "label": "原reranker", "mode": "baseline"},
    {"id": "rank_rrf", "label": "reranker＋召回排序RRF", "mode": "rank_rrf", "k": 20,
     "reranker_weight": 4., "retrieval_weight": 1.},
    {"id": "mmr085", "label": "reranker＋MMR", "mode": "mmr", "lambda": .85,
     "relative_score_floor": .2, "protect_top1": True},
    {"id": "source_cap2", "label": "reranker＋来源软限额", "mode": "source_cap",
     "window": 30, "top_k": 5, "per_source": 2, "relative_score_floor": .2,
     "protect_top1": True},
    {"id": "source_mmr", "label": "reranker＋显式来源保护＋MMR", "mode": "source_mmr",
     "lambda": .85, "relative_score_floor": .2, "protect_top1": True,
     "max_explicit_sources": 3, "minimum_explicit_sources": 2},
]

# Match document names/standard identifiers in the query, never evidence labels.
SOURCE_ALIASES = {
    "faa-phak": r"\bPHAK\b|Pilot(?:[’']s)? Handbook of Aeronautical Knowledge|飞行员航空知识手册",
    "faa-aim": r"\bAIM\b|Aeronautical Information Manual|航空信息手册",
    "faa-aviation-weather-handbook": r"\bAWH\b|Aviation Weather Handbook|航空气象手册",
    "faa-instrument-flying-handbook": r"\bIFH\b|Instrument Flying Handbook|仪表飞行手册",
    "faa-pilot-controller-glossary": r"\bPCG\b|Pilot[/／ -]Controller Glossary|飞行员[/／]管制员术语表",
    "eurocontrol-asterix-cat062": r"\bCAT\s*0?62\b|Category\s+0?62\b",
    "wmo-no8-volume1": r"\bWMO\b|世界气象组织",
    "ecmwf-era5-documentation": r"\bERA5\b",
    "scat-research-guide": r"SCAT科研解释指南|SCAT\s+research\s+guide",
    "gbar-method-curated": r"GBAR方法说明|GBAR\s+method\s+guide",
    "trajectory-evaluation-curated": r"TRAJECTORY_EVALUATION\.md|轨迹评测说明",
    "uav-research-guide": r"UAV科研解释指南|无人机科研解释指南",
}


def explicit_sources(query):
    matches = [(m.start(), sid) for sid, pattern in SOURCE_ALIASES.items()
               for m in [re.search(pattern, query, re.I)] if m]
    return [sid for _, sid in sorted(matches)]


def _mmr(base, scores, similarities, alpha, floor, prefix):
    eligible = [cid for cid in base if scores[cid] >= floor]
    chosen = list(prefix)
    used = set(chosen)
    positions = {cid: i for i, cid in enumerate(base)}
    while len(used) < len(eligible):
        remaining = [cid for cid in eligible if cid not in used]
        if not remaining:
            break
        def utility(cid):
            # Cosine is clipped at zero; opposite passages are not rewarded.
            redundancy = max((max(0., float(similarities[positions[cid], positions[other]]))
                              for other in chosen), default=0.)
            return alpha*scores[cid]-(1-alpha)*redundancy
        best = max(remaining, key=lambda cid: (utility(cid), -positions[cid]))
        chosen.append(best)
        used.add(best)
    return chosen+[cid for cid in base if cid not in used]


def rerank(policy, *, query, pool, scores, records, similarities=None):
    """No gold IDs, case IDs, cohorts, expected sources or model clients accepted.

    similarities must be indexed by the baseline reranker order, not pool order.
    Returns a full permutation plus rule activation details.
    """
    if policy not in POLICIES:
        raise ValueError("policy_not_predeclared")
    if not pool or len(pool) != len(set(pool)) or set(scores) != set(pool):
        raise ValueError("invalid_candidate_scores")
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
        raise ValueError("invalid_candidate_scores")
    if any(cid not in records for cid in pool):
        raise ValueError("candidate_metadata_missing")
    positions = {cid: i for i, cid in enumerate(pool)}
    base = sorted(pool, key=lambda cid: (-scores[cid], positions[cid]))
    info = {"explicit_sources": [], "reserved_source_candidates": [],
            "protect_top1": policy.get("protect_top1", False)}
    if policy["mode"] == "baseline":
        return base, info
    if policy["mode"] == "rank_rrf":
        ranks = {cid: i+1 for i, cid in enumerate(base)}
        k = policy["k"]
        result = sorted(pool, key=lambda cid: (
            -(policy["reranker_weight"]/(k+ranks[cid])+
              policy["retrieval_weight"]/(k+positions[cid]+1)), ranks[cid], positions[cid]))
        return result, info
    if scores[base[0]] <= 0:
        return base, info
    floor = policy["relative_score_floor"]*scores[base[0]]
    info["score_floor"] = floor
    if policy["mode"] == "source_cap":
        chosen = [base[0]]
        counts = Counter([records[base[0]]["source_id"]])
        for cid in base[1:policy["window"]]:
            sid = records[cid]["source_id"]
            if scores[cid] >= floor and counts[sid] < policy["per_source"]:
                chosen.append(cid)
                counts[sid] += 1
            if len(chosen) >= policy["top_k"]:
                break
        # Soft cap: if sufficient relevant diversity is absent, backfill as-is.
        used = set(chosen)
        result = chosen+[cid for cid in base if cid not in used]
        return result, info
    if similarities is None or getattr(similarities, "shape", None) != (len(base), len(base)):
        raise ValueError("similarity_matrix_missing_or_misaligned")
    prefix = [base[0]]
    if policy["mode"] == "source_mmr":
        requested = explicit_sources(query)
        info["explicit_sources"] = requested
        if len(requested) >= policy["minimum_explicit_sources"]:
            for sid in requested[:policy["max_explicit_sources"]]:
                best = next((cid for cid in base if records[cid]["source_id"] == sid and
                             scores[cid] >= floor), None)
                if best is not None and best not in prefix:
                    prefix.append(best)
                    info["reserved_source_candidates"].append(best)
    result = _mmr(base, scores, similarities, policy["lambda"], floor, prefix)
    assert len(result) == len(set(result)) == len(pool) and set(result) == set(pool)
    return result, info
