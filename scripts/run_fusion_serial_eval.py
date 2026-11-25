"""Serial fusion-only evaluation with original production BM25 kept intact."""
from __future__ import annotations
import datetime
import fcntl
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"scripts")]
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                  TOKENIZERS_PARALLELISM="false", RAGAS_DO_NOT_TRACK="true",
                  LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false", OMP_NUM_THREADS="4")
OUT = ROOT/"var/validation/20261125-rag-fusion-serial-v1"
BASE = ROOT/"output/rag-evidence-top100-20261125"
DEPTHS = (5, 10, 20, 50, 100)


def read(p):
    return json.loads(p.read_text(encoding="utf8"))


def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as stream:
        for b in iter(lambda: stream.read(8*1024*1024), b""):
            h.update(b)
    return h.hexdigest()


def save(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix+".writing")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf8")
    tmp.replace(p)


def aggregate(rows, stage):
    return {cohort: {str(k): {
        "cases": len(rs), "complete_cases": sum(r[stage][str(k)]["all_evidence"] for r in rs),
        "evidence_hits": sum(r[stage][str(k)]["evidence_hits"] for r in rs),
        "evidence_total": sum(len(r["gold"]) for r in rs),
    } for k in DEPTHS} for cohort in ["new120", "old113"]
        for rs in [[r for r in rows if r["cohort"] == cohort]]}


def main():
    assert not (OUT/"summary.json").exists(), "completed run preserved"
    lock = (OUT/"run.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    attempts = []

    def deny(*a, **kw):
        attempts.append("blocked")
        raise RuntimeError("network_disabled_for_serial_retrieval_evaluation")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
    plan = read(OUT/"preregistration.json")
    data = read(OUT/"dataset.json")["cases"]
    protected = {**plan["protected_files"], **plan["input_files_sha256"]}
    assert all(sha(ROOT/p) == h for p, h in protected.items()), "production/input drift"
    assert sha(ROOT/"src/rag_retrieval/baseline.py") == plan["original_bm25_sha256"]
    from fusion_policy_experiments import POLICIES, select
    from rag_retrieval.production import Retriever, load_config
    from rag_retrieval.runtime import M8_RRF_PROFILE
    from rag_retrieval.enhancements import rank_exact_identifier_matches
    from rag_retrieval.rerank_comparison import frozen_candidates, order_by_scores
    from evidence_retrieval_metrics import coverage
    import numpy as np
    import torch

    assert POLICIES == plan["policies"] and M8_RRF_PROFILE == plan["profile"]
    script_hashes = {name: sha(ROOT/"scripts"/name) for name in
                     ["run_fusion_serial_eval.py", "fusion_policy_experiments.py", "evidence_retrieval_metrics.py"]}
    run_identity = {"dataset_sha256": sha(OUT/"dataset.json"),
                    "preregistration_sha256": sha(OUT/"preregistration.json"),
                    "scripts_sha256": script_hashes}
    if (OUT/"run-identity.json").exists():
        assert read(OUT/"run-identity.json") == run_identity
    else:
        save(OUT/"run-identity.json", run_identity)
    started = time.time()
    cfg = load_config(ROOT, "services/rag/qwen3-production-v1.json")
    engine = Retriever(ROOT, cfg)
    index = engine.index
    assert sum(v < 0 for v in index.bm25._idf.values()) == 37
    baseline_new = {r["case_id"]: r for r in read(BASE/"results.json")["cases"]}
    dense_new = {r["case_id"]: r for r in read(BASE/"dense-pool-ablation.json")["results"]}
    leaves = {}
    for number, u in enumerate(data, 1):
        path = OUT/"leaves"/(u["id"]+".json")
        if path.exists():
            r = read(path)
            assert r["run_identity"] == run_identity
        else:
            q = " ".join(u["question"].split())
            v = np.asarray(index.model.encode([q], normalize_embeddings=True, show_progress_bar=False)[0], dtype=np.float32)
            dense = [index.chunk_ids[i] for i in np.argsort(-(index.corpus_vectors@v), kind="stable")]
            lexical = [cid for cid, _ in index.bm25.rank(q, limit=len(index.chunk_ids))]
            exact = [cid for cid, _ in rank_exact_identifier_matches(q, index.chunks, limit=200)]
            branches = {"dense": dense[:200], "lexical": lexical[:200], "exact": exact}
            if u["cohort"] == "new120":
                old = baseline_new[u["id"]]
                for stage, branch in [("dense", "dense"), ("lexical", "bm25"), ("exact", "exact")]:
                    assert branches[stage] == [x["chunk_id"] for x in old["rankings"][branch]]
            r = {"id": u["id"], "run_identity": run_identity, **branches,
                 "gold_leaf_ranks": {cid: {"dense": dense.index(cid)+1,
                     "bm25": lexical.index(cid)+1 if cid in lexical else None,
                     "exact": exact.index(cid)+1 if cid in exact else None} for cid in u["gold"]}}
            save(path, r)
        leaves[u["id"]] = r
        save(OUT/"progress.json", {"stage": "leaf_recall", "completed": number, "total": 233,
                                   "qa_calls": 0, "external_llm_calls": 0})
    summaries = []
    for policy in POLICIES:  # Deliberately serial: complete one policy before starting the next.
        results = []
        for number, u in enumerate(data, 1):
            path = OUT/"policies"/policy["id"]/(u["id"]+".json")
            if path.exists():
                entry = read(path)
                assert entry["run_identity"] == run_identity
            else:
                r = leaves[u["id"]]
                q = " ".join(u["question"].split())
                pool = select(policy, r["dense"], r["lexical"], r["exact"], index.chunks_by_id, q, M8_RRF_PROFILE)
                assert len(pool) == len(set(pool)) == 100
                seconds = None
                info = None
                scores = None
                origin = "fresh_local_rerank"
                if u["cohort"] == "new120" and policy["id"] in {"baseline", "dense100"}:
                    control = baseline_new[u["id"]] if policy["id"] == "baseline" else dense_new[u["id"]]
                    expected = ([x["chunk_id"] for x in control["rankings"]["candidate100"]]
                                if policy["id"] == "baseline" else control["candidate_pool"])
                    assert pool == expected
                    ranked = ([x["chunk_id"] for x in control["rankings"]["reranked"]]
                              if policy["id"] == "baseline" else control["ranking"])
                    origin = "saved_same_ordered_pool_same_model_control"
                else:
                    torch.cuda.synchronize()
                    t = time.perf_counter()
                    scores, info = engine.reranker.score(u["question"].strip(),
                        [index.chunks_by_id[cid].retrieval_text() for cid in pool])
                    torch.cuda.synchronize()
                    seconds = time.perf_counter()-t
                    assert not info["truncated_pairs"] and all(map(np.isfinite, scores))
                    ranked = order_by_scores(pool, scores)
                assert len(ranked) == 100 and set(ranked) == set(pool)
                parity = None
                if u["cohort"] == "old113" and policy["id"] == "baseline":
                    cached = read(ROOT/"output/rag-chart-rewrite-20261124/retrieval"/(u["id"]+".json"))
                    parity = ranked[:10] == [x["chunk_id"] for x in cached["results"][:10]]
                entry = {**u, "policy": policy["id"], "run_identity": run_identity,
                         "pool": pool, "ranking": ranked, "origin": origin,
                         "candidate_metrics": coverage(pool, u["gold"], u["groups"]),
                         "metrics": coverage(ranked, u["gold"], u["groups"]),
                         "gold_final_ranks": {cid: ranked.index(cid)+1 if cid in ranked else None for cid in u["gold"]},
                         "gold_leaf_ranks": r["gold_leaf_ranks"], "seconds": seconds,
                         "scores": dict(zip(pool, scores, strict=True)) if scores is not None else None,
                         "rerank_info": info, "old_baseline_top10_parity": parity}
                save(path, entry)
            results.append(entry)
            save(OUT/"progress.json", {"stage": "rerank", "policy": policy["id"],
                "cohort": u["cohort"], "completed": number, "total": 233,
                "qa_calls": 0, "external_llm_calls": 0})
            if number % 20 == 0:
                print(json.dumps({"policy": policy["id"], "completed": number}), flush=True)
        base = {r["id"]: r for r in read(OUT/"baseline-results.json")["cases"]} if policy["id"] != "baseline" else {r["id"]: r for r in results}
        paired = {cohort: {str(k): {
            "gained": [r["id"] for r in results if r["cohort"] == cohort and
                       r["metrics"][str(k)]["all_evidence"] and not base[r["id"]]["metrics"][str(k)]["all_evidence"]],
            "lost": [r["id"] for r in results if r["cohort"] == cohort and
                     not r["metrics"][str(k)]["all_evidence"] and base[r["id"]]["metrics"][str(k)]["all_evidence"]],
        } for k in (5, 10)} for cohort in ["new120", "old113"]}
        latency = {cohort: {"fresh_observations": len(values),
                   "p50_seconds": float(np.percentile(values, 50)) if values else None,
                   "p95_seconds": float(np.percentile(values, 95)) if values else None}
                   for cohort in ["new120", "old113"] for values in [[r["seconds"] for r in results
                       if r["cohort"] == cohort and r["seconds"] is not None]]}
        summary = {"policy": policy, "status": "completed", "cases": 233,
                   "metrics": aggregate(results, "metrics"),
                   "candidate_metrics": aggregate(results, "candidate_metrics"),
                   "paired_against_baseline": paired, "latency": latency,
                   "fresh_local_reranks": sum(r["origin"] == "fresh_local_rerank" for r in results),
                   "saved_control_ranks": sum(r["origin"] != "fresh_local_rerank" for r in results),
                   "old_baseline_top10_parity": sum(r["old_baseline_top10_parity"] is True for r in results),
                   "qa_calls": 0, "external_llm_calls": 0}
        save(OUT/(policy["id"]+"-results.json"), {"cases": results})
        save(OUT/(policy["id"]+"-summary.json"), summary)
        summaries.append(summary)
        print(json.dumps({"policy_completed": policy["id"], "metrics": summary["metrics"]}, ensure_ascii=False), flush=True)
        assert all(sha(ROOT/p) == h for p, h in protected.items()) and not attempts
    assert all(sha(ROOT/"scripts"/p) == h for p, h in script_hashes.items())
    save(OUT/"summary.json", {"status": "completed", "policies": summaries, "cases_per_policy": 233,
        "serial_order": [p["id"] for p in POLICIES], "run_identity": run_identity,
        "original_bm25_sha256": sha(ROOT/"src/rag_retrieval/baseline.py"),
        "original_bm25_negative_idf_terms": 37, "protected_files_unchanged": True,
        "production_changes": 0, "qa_calls": 0, "external_llm_calls": 0,
        "network_connections_attempted": len(attempts), "seconds": time.time()-started,
        "completed_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()})
    print(json.dumps({"status": "completed", "seconds": time.time()-started}), flush=True)


if __name__ == "__main__":
    main()
