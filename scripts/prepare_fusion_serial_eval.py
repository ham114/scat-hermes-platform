"""Freeze five serial policies and unchanged new/old retrieval labels."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT/"scripts")]
from fusion_policy_experiments import POLICIES

OUT = ROOT/"var/validation/20261125-rag-fusion-serial-v1"
BASE = ROOT/"output/rag-evidence-top100-20261125"


def read(p):
    return json.loads(p.read_text(encoding="utf8"))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source_plan = read(BASE/"plan.json")
    corpus = ROOT/"var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl"
    records = {r["chunk_id"]: r for r in map(json.loads, corpus.read_text(encoding="utf8").splitlines())}
    files = [ROOT/"output/ragas-multihop120-evaluation-20261124/dataset.json"]
    new = read(files[0])
    assert sha(files[0]) == source_plan["dataset_sha256"]
    units = [{"id": u["case_id"], "cohort": "new120", "split": "multihop120",
              "category": u["category"], "question": u["question"],
              "gold": u["gold_chunk_ids"], "groups": u["supporting_evidence_groups"]}
             for u in new["units"]]
    for name in ["evaluation.json", "regression.json"]:
        p = ROOT/"evaluations/rag_p0_reviewed_20261123_v1"/name
        files.append(p)
        for c in read(p)["cases"]:
            if c.get("turns"):
                continue
            groups = c["supporting_evidence_groups"]
            units.append({"id": c["case_id"], "cohort": "old113", "split": name,
                          "category": c["category"], "question": c["question"],
                          "gold": sorted({cid for g in groups for cid in g}), "groups": groups})
            cached = ROOT/"output/rag-chart-rewrite-20261124/retrieval"/(c["case_id"]+".json")
            assert read(cached)["query"] == c["question"]
            assert read(cached)["corpus_sha256"] == source_plan["corpus_sha256"]
            files.append(cached)
    assert len(units) == len({u["id"] for u in units}) == 233
    assert sum(u["cohort"] == "old113" for u in units) == 113
    assert all(u["gold"] and all(cid in records for cid in u["gold"]) for u in units)
    for name in ["plan.json", "results.json", "dense-pool-ablation.json"]:
        files.append(BASE/name)
    dataset = {"cases": units, "cohorts": {"new120": 120, "old113": 113},
               "corpus_sha256": sha(corpus), "metric": "strict frozen chunk-ID coverage, no QA"}
    plan = {"policies": POLICIES, "serial_order": [p["id"] for p in POLICIES],
            "candidate_limit": 100, "leaf_limit": 200,
            "profile": source_plan["profile"],
            "original_bm25_sha256": source_plan["source_sha256"]["src/rag_retrieval/baseline.py"],
            "protected_files": {**source_plan["source_sha256"], **source_plan["protected_files"]},
            "input_files_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in files},
            "control_reuse": "For new120 only, reuse saved baseline100 and dense100 rerank order iff freshly reconstructed ordered candidate pools match exactly; same frozen model/corpus. Old113 baseline and dense controls freshly scored.",
            "scoring": "Local Qwen relevance forward pass only; no generate(), no Ragas or external API.",
            "selection_rule": "Compare new120 and old113 separately, list every Top5/10 regression. No result-dependent parameter tuning or production deployment.",
            "qa_calls": 0, "external_llm_calls": 0, "production_changes": 0}
    for name, value in [("dataset.json", dataset), ("preregistration.json", plan)]:
        p = OUT/name
        text = json.dumps(value, ensure_ascii=False, indent=2)+"\n"
        if p.exists():
            assert p.read_text(encoding="utf8") == text, "frozen plan changed"
        else:
            p.write_text(text, encoding="utf8")
    print(json.dumps({"cases": len(units), "cohorts": dataset["cohorts"],
                      "gold_counts": {cohort: sum(len(u["gold"]) for u in units if u["cohort"] == cohort)
                                      for cohort in dataset["cohorts"]},
                      "serial_order": plan["serial_order"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
