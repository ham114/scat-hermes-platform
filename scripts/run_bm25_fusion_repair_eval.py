"""Frozen new-set evidence evaluation, with no generation or external clients.

Full runs require an isolated source snapshot on the existing GPU server.
Candidate-only runs can reuse the audited frozen leaf rankings on a CPU.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("RAG_REPAIR_SOURCE", ROOT / "src")).resolve()
sys.path[:0] = [str(SOURCE), str(ROOT / "scripts")]
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                  HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                  LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false",
                  RAGAS_DO_NOT_TRACK="true", OMP_NUM_THREADS="4")
BASE = ROOT / "output/rag-evidence-top100-20261125"
OUT = ROOT / "var/validation/20261125-rag-bm25-fusion-repair"
DATA = ROOT / "output/ragas-multihop120-evaluation-20261124/dataset.json"
DEPTHS = (5, 10, 20, 50, 100, 150)


def read(p):
    return json.loads(p.read_text(encoding="utf8"))


def sha(p):
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix(p.suffix+".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n",
                         encoding="utf8")
    temporary.replace(p)


def aggregate(rows, stage):
    return {str(k): {
        "complete_cases": sum(r["metrics"][stage][str(k)]["all_evidence"] for r in rows),
        "evidence_hits": sum(r["metrics"][stage][str(k)]["evidence_hits"] for r in rows),
    } for k in DEPTHS}


def candidate_rows(rows, fixed, units, chunks, profile):
    from evidence_retrieval_metrics import coverage
    from rag_retrieval.candidates import LEGACY_POLICY, RETAIN_DENSE_POLICY, select_candidates

    results = []
    for r, old in zip(rows, fixed):
        assert r["case_id"] == old["case_id"]
        dense = [e["chunk_id"] for e in r["rankings"]["dense"]]
        exact = [e["chunk_id"] for e in r["rankings"]["exact"]]
        lexical = old["ranks"]["corrected_bm25"]
        kwargs = dict(query=" ".join(r["question"].split()), dense=dense, lexical=lexical,
                      exact=exact, chunks_by_id=chunks, profile=profile)
        selection = select_candidates(**kwargs, policy=RETAIN_DENSE_POLICY, limit=150)
        legacy = select_candidates(**kwargs, policy=LEGACY_POLICY, limit=100)
        assert list(legacy.ids) == old["ranks"]["corrected_candidate100"]
        assert set(dense[:100]) <= set(selection.ids)
        assert len(selection.ids) == 150 and len(set(selection.ids)) == 150
        u = units[r["case_id"]]
        results.append({
            "case_id": r["case_id"], "category": r["category"], "question": r["question"],
            "gold_chunk_ids": u["gold_chunk_ids"], "pool": list(selection.ids),
            "signals": selection.signals,
            "gold_ranks": {cid: selection.signals.get(cid) for cid in u["gold_chunk_ids"]},
            "metrics": {"candidate150": coverage(selection.ids, u["gold_chunk_ids"],
                                                    u["supporting_evidence_groups"], DEPTHS)},
        })
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates-only", action="store_true")
    args = parser.parse_args()
    attempts = []

    def deny(*a, **kw):
        attempts.append("blocked")
        raise RuntimeError("network_disabled_for_retrieval_only_evaluation")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
    from rag_retrieval.baseline import Chunk
    from evidence_retrieval_metrics import coverage

    started = time.time()
    old_plan = read(BASE / "plan.json")
    data = read(DATA)
    assert sha(DATA) == old_plan["dataset_sha256"]
    rows = read(BASE / "results.json")["cases"]
    df = read(BASE / "bm25-df-counterfactual.json")
    assert df["input_results_sha256"] == sha(BASE / "results.json")
    units = {u["case_id"]: u for u in data["units"]}
    assert len(rows) == len(units) == 120
    corpus = ROOT / "var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl"
    assert sha(corpus) == old_plan["corpus_sha256"]
    chunks = {}
    for line in corpus.read_text(encoding="utf8").splitlines():
        r = json.loads(line)
        chunks[r["chunk_id"]] = Chunk(r["chunk_id"], r["source_id"], r["source_version"],
                                      tuple(r.get("section_path", [])), r["text"],
                                      tuple(r.get("exact_terms", [])))
    frozen_inputs = {str(p.relative_to(ROOT)): sha(p) for p in
                     [DATA, BASE/"results.json", BASE/"bm25-df-counterfactual.json",
                      BASE/"dense-pool-ablation.json", corpus]}
    results = candidate_rows(rows, df["results"], units, chunks, old_plan["profile"])
    source_hashes = {str(p.relative_to(SOURCE)): sha(p)
                     for p in sorted((SOURCE/"rag_retrieval").glob("*.py"))}
    candidate_summary = {
        "status": "candidates_completed_rerank_pending", "cases": 120,
        "evidence_instances": 240, "by_stage": {"candidate150": aggregate(results, "candidate150")},
        "source_sha256": source_hashes, "inputs_sha256": frozen_inputs,
        "all_dense100_preserved": True, "corrected_legacy_pool_parity_cases": 120,
        "qa_calls": 0, "external_llm_calls": 0, "network_connections_attempted": len(attempts),
        "protocol": "Candidate-only stage reuses audited frozen dense200, corrected BM25-DF200 and exact200; no new model inference.",
    }
    save(OUT/"candidate-results.json", {"cases": results})
    save(OUT/"candidate-summary.json", candidate_summary)
    print(json.dumps(candidate_summary, ensure_ascii=False), flush=True)
    if args.candidates_only:
        return

    assert SOURCE != (ROOT/"src").resolve(), "GPU runs must use an isolated source snapshot"
    assert not (OUT/"summary.json").exists(), "completed evaluation is immutable"
    import fcntl
    lock = (OUT/"run.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    protected = {**old_plan["protected_files"], **old_plan["source_sha256"]}
    assert all(sha(ROOT/p) == h for p, h in protected.items()), "production/source drift"
    from rag_retrieval.production import Retriever, load_config
    from rag_retrieval.candidates import build_candidates, LEGACY_POLICY, RETAIN_DENSE_POLICY
    from rag_retrieval.runtime import M8_RRF_PROFILE
    from rag_retrieval.rerank_comparison import order_by_scores
    import numpy as np
    import torch

    assert M8_RRF_PROFILE == old_plan["profile"]
    import rag_retrieval.baseline
    assert Path(rag_retrieval.baseline.__file__).resolve().is_relative_to(SOURCE)
    config_path = SOURCE.parent / "config.json"
    cfg = load_config(ROOT, str(config_path.relative_to(ROOT)))
    assert cfg["candidate_policy"] == RETAIN_DENSE_POLICY and cfg["candidate_limit"] == 150
    save(OUT/"plan.json", {"source_root": str(SOURCE), "source_sha256": source_hashes,
                          "protected_files": protected, "inputs_sha256": frozen_inputs,
                          "config_sha256": sha(config_path), "cases": 120,
                          "benchmark_protocol": "12 fixed evenly spaced questions, two paired repeats, alternating order, local Qwen100 versus150; excludes queueing and query encoding.",
                          "qa_calls": 0, "external_llm_calls": 0})
    engine = Retriever(ROOT, cfg)
    assert all(v > 0 for v in engine.index.bm25._idf.values())
    warmup, _ = engine.reranker.score(rows[0]["question"],
                                      [chunks[c].retrieval_text() for c in results[0]["pool"]])
    assert len(warmup) == 150
    fresh = []
    for number, (r, candidate, old) in enumerate(zip(rows, results, df["results"]), 1):
        path = OUT / "cases" / (r["case_id"]+".json")
        if path.exists():
            entry = read(path)
            assert entry["source_sha256"] == source_hashes
            assert entry["inputs_sha256"] == frozen_inputs
        else:
            t = time.perf_counter()
            selection = build_candidates(engine.index, r["question"], M8_RRF_PROFILE,
                                         policy=RETAIN_DENSE_POLICY, limit=150)
            recall_seconds = time.perf_counter()-t
            assert list(selection.ids) == candidate["pool"], "fresh candidate parity failure"
            assert engine.index.bm25.rank(" ".join(r["question"].split()), limit=200) and \
                [cid for cid, _ in engine.index.bm25.rank(" ".join(r["question"].split()), limit=200)] == old["ranks"]["corrected_bm25"]
            torch.cuda.synchronize()
            t = time.perf_counter()
            scores, info = engine.reranker.score(r["question"].strip(),
                                                [chunks[c].retrieval_text() for c in selection.ids])
            torch.cuda.synchronize()
            score_seconds = time.perf_counter()-t
            assert not info["truncated_pairs"] and all(map(np.isfinite, scores))
            ranked = order_by_scores(selection.ids, scores)
            u = units[r["case_id"]]
            entry = {**candidate, "ranking": ranked,
                     "scores": dict(zip(selection.ids, scores, strict=True)),
                     "metrics": {**candidate["metrics"], "reranked150": coverage(
                         ranked, u["gold_chunk_ids"], u["supporting_evidence_groups"], DEPTHS)},
                     "gold_final_ranks": {cid: ranked.index(cid)+1 if cid in ranked else None
                                           for cid in u["gold_chunk_ids"]},
                     "source_sha256": source_hashes, "inputs_sha256": frozen_inputs,
                     "recall_seconds": recall_seconds, "rerank_seconds": score_seconds,
                     "reranker_input_info": info}
            save(path, entry)
        fresh.append(entry)
        save(OUT/"progress.json", {"status": "reranking", "completed": number,
                                   "total": 120, "qa_calls": 0, "external_llm_calls": 0})
        if number % 10 == 0:
            print(json.dumps({"completed": number}, ensure_ascii=False), flush=True)
    # These are latency probes, not a second question-answer evaluation.
    benchmark = []
    for pos in np.linspace(0, 119, 12, dtype=int):
        r = rows[pos]
        pools = {"fixed_mixed100": df["results"][pos]["ranks"]["corrected_candidate100"],
                 "dense150": fresh[pos]["pool"]}
        for repeat in range(2):
            for name in (["fixed_mixed100", "dense150"] if repeat == 0 else ["dense150", "fixed_mixed100"]):
                torch.cuda.synchronize()
                t = time.perf_counter()
                _, info = engine.reranker.score(r["question"].strip(),
                    [chunks[c].retrieval_text() for c in pools[name]])
                torch.cuda.synchronize()
                assert not info["truncated_pairs"]
                benchmark.append({"case_id": r["case_id"], "repeat": repeat, "arm": name,
                                  "seconds": time.perf_counter()-t, "pairs": len(pools[name])})
        print(json.dumps({"benchmark_cases_completed": len(benchmark)//4}), flush=True)
    paired = {name: {str(k): {
        "gained_cases": [r["case_id"] for r, old in zip(fresh, controls)
                          if r["metrics"]["reranked150"][str(k)]["all_evidence"]
                          and not old[metric][str(k)]["all_evidence"]],
        "lost_cases": [r["case_id"] for r, old in zip(fresh, controls)
                        if not r["metrics"]["reranked150"][str(k)]["all_evidence"]
                        and old[metric][str(k)]["all_evidence"]],
    } for k in DEPTHS[:5]} for name, controls, metric in [
        ("production100", [{"m": r["metrics"]["reranked"]} for r in rows], "m"),
        ("corrected100", [{"m": r["metrics"]["corrected_reranked"]} for r in df["results"]], "m"),
        ("dense100", [{"m": r["metrics"]} for r in read(BASE/"dense-pool-ablation.json")["results"]], "m"),
    ]}
    assert all(sha(ROOT/p) == h for p, h in {**protected, **frozen_inputs}.items())
    assert source_hashes == {str(p.relative_to(SOURCE)): sha(p)
                             for p in sorted((SOURCE/"rag_retrieval").glob("*.py"))}
    assert not attempts
    summary = {"status": "completed", "cases": 120, "evidence_instances": 240,
               "by_stage": {stage: aggregate(fresh, stage)
                            for stage in ["candidate150", "reranked150"]},
               "paired_against_saved_controls": paired,
               "controls": "Historical frozen same-question, same corpus/model controls; candidate selection freshly verified in all120. Historical controls were not re-answered.",
               "benchmark": {arm: {"observations": len(values),
                                     "p50_seconds": float(np.percentile(values, 50)),
                                     "p95_seconds": float(np.percentile(values, 95))}
                             for arm in ["fixed_mixed100", "dense150"]
                             for values in [[b["seconds"] for b in benchmark if b["arm"] == arm]]},
               "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
               "gpu_peak_reserved_bytes": torch.cuda.max_memory_reserved(),
               "fresh_candidate_parity_cases": 120, "protected_files_unchanged": True,
               "all_dense100_preserved": True, "truncated_pairs": 0,
               "production_changes": 0, "qa_calls": 0, "external_llm_calls": 0,
               "network_connections_attempted": len(attempts),
               "source_sha256": source_hashes, "inputs_sha256": frozen_inputs,
               "seconds": time.time()-started,
               "completed_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()}
    save(OUT/"results.json", {"cases": fresh})
    save(OUT/"benchmark.json", {"observations": benchmark})
    save(OUT/"summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
