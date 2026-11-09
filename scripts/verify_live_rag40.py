"""Independently check frozen native cases, actual receipts, and retrieval reproducibility."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261026-rag-live/test40"
GEN = Path("/home/h3c/project/python/ragas-evaluation/reports/batch-20261026-live40")
DATA = ROOT / "evaluations/rag_live40_ragas_20261026_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    import httpx

    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
    from verify_real_agent_rag import verify_result

    from hermes_runtime.real_rag import load_corpus

    config = read(ROOT / "services/rag/real-agent-production-v1.json")
    records = load_corpus(config)
    prereg = read(OUT / "preregistration.json")
    if (
        any(sha(ROOT / p) != digest for p, digest in prereg["sources"].items())
        or sha(DATA / "evaluation.json") != prereg["dataset_sha256"]
    ):
        raise ValueError("frozen_source_or_dataset_changed")
    summary = read(OUT / "summary.json")
    repaired = read(OUT / "repair-v2/summary.json")
    cases = read(DATA / "evaluation.json")["cases"]
    candidates = [json.loads(x) for x in (GEN / "candidates.jsonl").read_text().splitlines()]
    if len(cases) != 40 or len(candidates) != 40 or len({c["case_id"] for c in cases}) != 40:
        raise ValueError("not_forty_unique_cases")
    normalized = set()
    old_questions = [
        json.loads(x)["question"]
        for x in (GEN.parent / "batch-20261025-dev40/user-review-20261026/candidates.jsonl")
        .read_text()
        .splitlines()
    ]
    old_questions += [
        x["user_input"] for x in read(GEN.parent / "20261025-184341-04969beb/testset.raw.json")
    ]
    normalize = lambda text: "".join(c.casefold() for c in text if c.isalnum())
    previous = {normalize(q) for q in old_questions}
    quote_count = 0
    for candidate in candidates:
        generated_path = (GEN.parents[1] / candidate["generation"]["raw_batch"]).resolve()
        if not generated_path.is_relative_to(GEN.resolve()):
            raise ValueError("candidate_generation_path_outside_batch")
        raw = read(generated_path)[candidate["generation"]["raw_index"]]
        if (
            raw["user_input"] != candidate["question"]
            or raw["reference"] != candidate["reference"]
            or raw["synthesizer_name"]
            not in {"single_hop_specific_query_synthesizer", "multi_hop_specific_query_synthesizer"}
        ):
            raise ValueError("not_preserved_native_ragas_output")
        question = normalize(candidate["question"])
        if question in normalized or question in previous:
            raise ValueError("exact_duplicate_of_known_question")
        normalized.add(question)
        for claim in candidate["claims"]:
            for citation in claim["evidence"]:
                source = records[citation["chunk_id"]]
                if (
                    any(
                        source[k] != citation[k]
                        for k in ("source_id", "source_version", "text_sha256")
                    )
                    or source["text"][citation["start_char"] : citation["end_char"]]
                    != citation["quote"]
                ):
                    raise ValueError("gold_evidence_identity_or_offset_mismatch")
                quote_count += 1
    provenance = []
    sources = set()
    for case in cases:
        for mode in ("fast", "deep"):
            folder = OUT / (case["case_id"] + "-" + mode)
            if not folder.exists():
                if mode == "deep" and case["case_id"] not in prereg["deep_case_ids"]:
                    continue
                raise ValueError("missing_preregistered_answer")
            result = read(folder / "native-api-result.json")
            receipt_rows = read(folder / "knowledge-receipts.json")
            if result["status"] != "completed" or not receipt_rows:
                recorded = next(
                    x
                    for x in summary["rows"]
                    if x["case_id"] == case["case_id"] and x["mode"] == mode
                )
                if result["status"] != "failed" or recorded["status"] != "failed" or receipt_rows:
                    raise ValueError("unaccounted_terminal_failure")
                provenance.append(
                    {
                        "case_id": case["case_id"],
                        "mode": mode,
                        "verified_tool_evidence": 0,
                        "execution_passed": False,
                        "terminal_failure_preserved": True,
                    }
                )
                continue
            verified = 0
            for payload in receipt_rows:
                data = payload["data"]
                if "results" in data:
                    if data["corpus_sha256"] != config["corpus_sha256"]:
                        raise ValueError("wrong_live_corpus")
                    for hit in data["results"]:
                        row = records[hit["chunk_id"]]
                        if any(
                            row[k] != hit[k]
                            for k in ("source_id", "source_version", "text_sha256", "text")
                        ):
                            raise ValueError("retrieved_source_changed")
                        sources.add(hit["source_id"])
                        verified += 1
                else:
                    if hashlib.sha256(data["answer"].encode()).hexdigest() != data["answer_sha256"]:
                        raise ValueError("deep_answer_changed")
                    for claim in data["claims"]:
                        for citation in claim["evidence"]:
                            ref = citation["reference"]
                            row = records[ref["chunk_id"]]
                            if (
                                any(
                                    row[k] != ref[k]
                                    for k in ("source_id", "source_version", "text_sha256")
                                )
                                or ref["corpus_sha256"] != config["corpus_sha256"]
                                or row["text"][citation["start_char"] : citation["end_char"]]
                                != citation["quote"]
                            ):
                                raise ValueError("deep_citation_changed")
                            sources.add(ref["source_id"])
                            verified += 1
            provenance.append(
                {
                    "case_id": case["case_id"],
                    "mode": mode,
                    "verified_tool_evidence": verified,
                    "execution_passed": True,
                }
            )
    nested = []
    state = (
        ROOT
        / "var/rag-operations"
        / hashlib.sha256(str(ROOT / "var/hermes/rag-production").encode()).hexdigest()[:20]
        / "invocations.sqlite"
    )
    with sqlite3.connect(state) as db:
        for scope, output, status in db.execute("SELECT scope,output,status FROM rag_invocations"):
            if status != "succeeded":
                raise ValueError("production_deep_operation_unresolved")
            folder = Path(output)
            report = read(folder / "result.json")
            local = config | {"scope": scope}
            nested.append(verify_result(report, records, local))
    review_counts = {
        "fast": {"pass": 0, "fail": 0, "unavailable": 0},
        "deep": {"pass": 0, "fail": 0, "unavailable": 0},
    }
    for case in cases:
        path = OUT / "repair-v2" / (case["case_id"] + "-review.json")
        review = read(path if path.exists() else OUT / (case["case_id"] + "-review.json"))
        for mode in ("fast", "deep"):
            if mode == "deep" and case["case_id"] not in prereg["deep_case_ids"]:
                continue
            item = review.get("by_mode", {}).get(mode)
            if item is None:
                review_counts[mode]["unavailable"] += 1
                continue
            allowed = {f"G{i}" for i in range(len(case["required_claims"]))}
            if (
                any(
                    not isinstance(item[key], list)
                    or any(not isinstance(x, str) for x in item[key])
                    for key in (
                        "covered_gold_ids",
                        "unsupported_claims",
                        "numeric_or_entity_errors",
                    )
                )
                or type(item["citations_adequate"]) is not bool
                or len(item["covered_gold_ids"]) != len(set(item["covered_gold_ids"]))
                or not set(item["covered_gold_ids"]) <= allowed
            ):
                raise ValueError("invalid_semantic_review_schema")
            if item["verdict"] == "pass" and (
                set(item["covered_gold_ids"]) != allowed
                or item["unsupported_claims"]
                or item["numeric_or_entity_errors"]
                or not item["citations_adequate"]
            ):
                raise ValueError("inconsistent_semantic_review_pass")
            review_counts[mode][item["verdict"]] += 1
    original = {x["case_id"]: x for x in read(OUT / "retrieval-progress.json")}
    replay = []
    with httpx.Client(trust_env=False, timeout=20) as client:
        for case in cases:
            result = client.post(
                "http://127.0.0.1:18133/search", json={"query": case["question"], "limit": 10}
            )
            result.raise_for_status()
            data = result.json()
            ids = [r["chunk_id"] for r in data["results"]]
            if data["corpus_sha256"] != config["corpus_sha256"]:
                raise ValueError("replay_corpus_changed")
            for k in (5, 10):
                measured = sum(
                    set(group) <= set(ids[:k]) for group in case["evidence_groups"]
                ) / len(case["evidence_groups"])
                if abs(measured - original[case["case_id"]]["group_recall_at_" + str(k)]) > 1e-12:
                    raise ValueError("retrieval_reproduction_mismatch")
            replay.append(
                {
                    "case_id": case["case_id"],
                    "ranked_chunk_ids": ids,
                    "scores": [r["score"] for r in data["results"]],
                    "latency_ms": data["latency_ms"],
                }
            )
    (OUT / "retrieval-replay-private.json").write_text(json.dumps(replay, indent=2))
    for mode in ("fast", "deep"):
        if (
            review_counts[mode]["pass"] != repaired["validated_model_review"][mode]["pass"]
            or review_counts[mode]["unavailable"]
            != repaired["validated_model_review"][mode]["unavailable"]
        ):
            raise ValueError("public_model_review_count_mismatch")
    report = {
        "status": "passed",
        "frozen_native_cases": 40,
        "native_outputs_preserved": True,
        "human_approved": 0,
        "gold_quotes_verified": quote_count,
        "live_runs_verified": len(provenance),
        "initial_successful_runs": sum(x["execution_passed"] for x in provenance),
        "initial_terminal_failures_preserved": sum(not x["execution_passed"] for x in provenance),
        "supplemental_known_terminal_retry_passed": repaired["terminal_api_retry"]["passed"],
        "tool_evidence_entries_verified": sum(x["verified_tool_evidence"] for x in provenance),
        "nested_four_agent_operations_verified": len(nested),
        "nested_quotes_verified": sum(x["citations_verified"] for x in nested),
        "research_parallel_and_role_checks": all(x["execution_passed"] for x in nested),
        "model_review_counts": review_counts,
        "retrieval_reproduced_queries": len(replay),
        "replay_p50_ms": statistics.median(x["latency_ms"] for x in replay),
        "replay_is_separate_after_test": True,
        "first_run_ranked_ids_were_not_saved": True,
        "external_model_requests": 0,
        "all_pinned_sources_unchanged": True,
        "unique_returned_sources": sorted(sources),
        "independent_human_admission": False,
    }
    (ROOT / "docs/validation/20261026-rag-live-test40-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
