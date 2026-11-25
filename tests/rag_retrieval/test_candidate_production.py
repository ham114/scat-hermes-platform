import hashlib
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import numpy as np
import pytest

from rag_retrieval.baseline import Chunk
from rag_retrieval.candidates import LEGACY_POLICY, RETAIN_DENSE_POLICY, WEIGHTED_POLICY
from rag_retrieval.production import Retriever, load_config


@pytest.mark.parametrize("profile,limit,policy", [
    ("m8_rrf_qwen06b_top100", 100, LEGACY_POLICY),
    ("m8_rrf_qwen06b_dense100_plus50", 150, RETAIN_DENSE_POLICY),
    ("m8_weighted2_qwen06b_rank_rrf_top100", 100, WEIGHTED_POLICY),
])
def test_config_binds_explicit_profile_to_policy_and_limit(tmp_path, profile, limit, policy):
    cfg = {"schema_version": 1, "profile_id": profile, "candidate_limit": limit,
           "candidate_policy": policy, "batch_size": 4, "max_input_tokens": 2048,
           "host": "127.0.0.1", "port": 18134, "max_inflight": 2}
    if policy == WEIGHTED_POLICY:
        cfg["ranking_policy"] = "rank_rrf_4_1_k20"
    for name in ["corpus", "embedding_cache", "model_manifest"]:
        p = tmp_path / name
        p.write_bytes(b"test")
        cfg[name] = str(p) if name != "model_manifest" else name
        cfg[name+"_sha256"] = hashlib.sha256(b"test").hexdigest()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    assert load_config(tmp_path, "config.json")["candidate_limit"] == limit
    cfg["candidate_limit"] = 150 if limit == 100 else 100
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="unapproved"):
        load_config(tmp_path, "config.json")
    cfg["candidate_limit"] = limit
    cfg["candidate_policy"] = "unknown"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="unapproved_candidate_policy"):
        load_config(tmp_path, "config.json")


def test_search_uses_shared_dense_retention_and_exposes_branch_ranks():
    ids = [f"d{i}" for i in range(200)]+[f"l{i}" for i in range(200)]
    chunks = [Chunk(cid, "source", "v7", (), cid) for cid in ids]
    records = {cid: {"chunk_id": cid, "source_id": "source", "source_version": "v7",
                     "text": cid, "text_sha256": "test"} for cid in ids}
    scored = []

    def score(query, documents):
        chunk_ids = [doc.splitlines()[-1] for doc in documents]
        scored.extend(chunk_ids)
        return [float(cid == "d99") for cid in chunk_ids], {"truncated_pairs": 0}

    engine = Retriever.__new__(Retriever)
    engine.index = SimpleNamespace(
        bm25=SimpleNamespace(rank=lambda *a, **kw: [(cid, 1.) for cid in ids[200:]]),
        model=SimpleNamespace(encode=lambda *a, **kw: np.array([[1.]])),
        corpus_vectors=np.arange(400, 0, -1).reshape(-1, 1), chunk_ids=ids,
        chunks=chunks, chunks_by_id={c.chunk_id: c for c in chunks},
    )
    engine.candidate_policy = RETAIN_DENSE_POLICY
    engine.candidate_limit = 150
    engine.lock = Lock()
    engine.records = records
    engine.reranker = SimpleNamespace(score=score)
    result = engine.search("aviation", 5)
    assert len(scored) == 150 and set(ids[:100]) <= set(scored)
    assert len(result) == 5 and result[0]["chunk_id"] == "d99"
    assert result[0]["signals"]["protected_dense"]
    assert result[0]["signals"]["dense_rank"] == 100
