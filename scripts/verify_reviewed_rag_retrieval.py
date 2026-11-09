"""Recompute saved retrieval results without rerunning a model or editing labels."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(groups, ranked):
    positions = {chunk: i for i, chunk in enumerate(ranked, 1)}
    completed = []
    relevant = set()
    for group in groups:
        ranks = []
        for bundle in group["alternatives"]:
            relevant.update(bundle)
            if set(bundle) <= positions.keys():
                ranks.append(max(positions[c] for c in bundle))
        completed.append(min(ranks) if ranks else math.inf)
    first = min(completed)
    dcg = sum(1 / math.log2(i + 1) for i, c in enumerate(ranked[:10], 1) if c in relevant)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(relevant), 10) + 1))
    return {
        "group_recall_at_5": sum(r <= 5 for r in completed) / len(completed),
        "group_recall_at_10": sum(r <= 10 for r in completed) / len(completed),
        "complete_at_5": float(all(r <= 5 for r in completed)),
        "complete_at_10": float(all(r <= 10 for r in completed)),
        "group_mrr": 1 / first if math.isfinite(first) else 0,
        "accepted_evidence_ndcg_at_10": dcg / ideal,
        "candidate_group_recall_at_100": sum(r <= 100 for r in completed) / len(completed),
    }


def percentile(values, q):
    values = sorted(values)
    index = (len(values) - 1) * q
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def close(a, b):
    if not math.isclose(a, b, abs_tol=1e-10, rel_tol=1e-10):
        raise ValueError("independent_metric_mismatch")


def main():
    if Path(sys.prefix).resolve() != Path("/home/h3c/anaconda3/envs/scat-agent"):
        raise ValueError("use_existing_scat_agent")
    directory = ROOT / "var/validation/20261026-reviewed-rag"
    prereg = json.loads((directory / "preregistration.json").read_text())
    config_path = ROOT / "services/rag/qwen3-reviewed-dev40-v1.json"
    if sha(config_path) != prereg["config_sha256"]:
        raise ValueError("config_changed_after_registration")
    config = json.loads(config_path.read_text())
    for name, row in config["inputs"].items():
        if sha(Path(row["path"])) != row["sha256"]:
            raise ValueError("input_changed:" + name)
    for name, expected in (config["retriever_sources"] | prereg["sources"]).items():
        if sha(ROOT / name) != expected:
            raise ValueError("source_changed:" + name)
    dataset = ROOT / "evaluations/rag_dev40_userreview_v1"
    manifest = json.loads((dataset / "manifest.json").read_text())
    if sha(dataset / "manifest.json") != prereg["dataset_manifest_sha256"]:
        raise ValueError("dataset_manifest_changed")
    for name, expected in manifest["files"].items():
        if sha(dataset / name) != expected:
            raise ValueError("dataset_file_changed")
    report = json.loads(
        (ROOT / "docs/validation/20261026-reviewed-dev40-comparison.json").read_text()
    )
    run = ROOT / "var/validation" / report["run_id"]
    rows = json.loads((run / "per-query-private.json").read_text())
    cases = {
        c["case_id"]: c for c in json.loads((dataset / "evaluation.json").read_text())["cases"]
    }
    labels = {
        c["case_id"]: c["document_claim_groups"]
        for c in json.loads((dataset / "claim_groups.json").read_text())["cases"]
    }
    if len(rows) != 40 or len({r["case_id"] for r in rows}) != 40 or set(cases) != set(labels):
        raise ValueError("comparison_case_set_incomplete")
    recalculated = {"baseline": [], "candidate": []}
    improved, regressed, incomplete = [], [], []
    for row in rows:
        case = cases[row["case_id"]]
        question = " ".join(case["question"].split())
        if hashlib.sha256(question.encode()).hexdigest() != row["query_sha256"]:
            raise ValueError("query_text_changed")
        original, candidate = row["baseline_ranking"], row["candidate_ranking"]
        scores = row["scores_in_original_order"]
        if len(original) != len(set(original)) or len(original) != 100:
            raise ValueError("invalid_candidate_count")
        if (
            set(candidate) != set(original)
            or len(scores) != 100
            or not all(map(math.isfinite, scores))
        ):
            raise ValueError("invalid_candidate_pool_or_scores")
        expected_order = sorted(range(100), key=lambda i: (-scores[i], i))
        if [original[i] for i in expected_order] != candidate:
            raise ValueError("rank_sort_or_tie_mismatch")
        for arm, ids in [("baseline", original), ("candidate", candidate)]:
            measured = metrics(labels[row["case_id"]], ids)
            for key, value in measured.items():
                close(value, row[arm][key])
            recalculated[arm].append(measured)
        if row["candidate"]["group_recall_at_5"] > row["baseline"]["group_recall_at_5"] + 1e-12:
            improved.append(row["case_id"])
        if row["candidate"]["group_recall_at_5"] < row["baseline"]["group_recall_at_5"] - 1e-12:
            regressed.append(row["case_id"])
        if row["candidate"]["complete_at_10"] != 1:
            incomplete.append(row["case_id"])
        close(row["retrieval_ms"] + row["rerank_ms"], row["total_candidate_ms"])
    for arm, values in recalculated.items():
        for key in values[0]:
            close(statistics.fmean(v[key] for v in values), report[arm][key])
    for category, group in report["by_category"].items():
        selected = [r for r in rows if r["category"] == category]
        if len(selected) != group["count"]:
            raise ValueError("category_count_mismatch")
        for arm in ("baseline", "candidate"):
            for key in report[arm]:
                close(statistics.fmean(r[arm][key] for r in selected), group[arm][key])
    for label, field in [
        ("baseline_retrieval_ms", "retrieval_ms"),
        ("rerank_only_ms", "rerank_ms"),
        ("candidate_total_ms", "total_candidate_ms"),
    ]:
        values = [r[field] for r in rows]
        close(percentile(values, 0.5), report["latency"][label]["p50"])
        close(percentile(values, 0.95), report["latency"][label]["p95"])
    if (
        len(improved) != report["r5_improved_queries"]
        or len(regressed) != report["r5_regressed_queries"]
    ):
        raise ValueError("query_delta_count_mismatch")
    if report["api_calls"] != 0 or report["qa_started"] or report["production_service_changed"]:
        raise ValueError("question_answer_stop_boundary_breached")
    result = {
        "state": "passed",
        "run_id": report["run_id"],
        "independently_recomputed_queries": 40,
        "candidate_pools_and_tie_order_checked": 40,
        "all_fixed_inputs_and_sources_unchanged": True,
        "labels_frozen_before_retrieval": True,
        "labels_modified_after_results": False,
        "dataset_version": manifest["dataset_version"],
        "dataset_groups": sum(map(len, labels.values())),
        "r5_improved_case_ids": improved,
        "r5_regressed_case_ids": regressed,
        "candidate_incomplete_at_10_case_ids": incomplete,
        "report_sha256": sha(ROOT / "docs/validation/20261026-reviewed-dev40-comparison.json"),
        "private_rows_sha256": sha(run / "per-query-private.json"),
        "external_api_calls": 0,
        "qa_started": False,
        "stop_point": "before-question-answer-validation",
    }
    output = ROOT / "docs/validation/20261026-reviewed-dev40-verification.json"
    if output.exists():
        raise ValueError("verification_already_exists")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
