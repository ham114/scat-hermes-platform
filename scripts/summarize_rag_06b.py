"""Independently verify all 40 final-runtime pairs, retaining failed earlier phases."""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "20261026-rag06b-v3-"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
    from verify_real_agent_rag import verify_result

    from hermes_runtime.real_rag import load_config, load_corpus

    allocation = read(ROOT / "services/rag/06b-continuation-allocation.json")
    primary = ROOT / "var/validation" / (PREFIX + "dev40-shard-1/frozen-sources")
    source = primary if primary.exists() else ROOT
    if any(sha(source / p) != v for p, v in allocation["source_runtime"].items()):
        raise ValueError("primary_runtime_changed")
    dataset = read(ROOT / "evaluations/rag_dev40_userreview_v1/evaluation.json")
    cases = {c["case_id"][-3:]: c for c in dataset["cases"]}
    labels = {
        c["case_id"][-3:]: c["document_claim_groups"]
        for c in read(ROOT / "evaluations/rag_dev40_userreview_v1/claim_groups.json")["cases"]
    }
    selection = {n: "dev40-shard-1" for n in ("001", "002", "003", "004", "005")}
    selection.update({n: "dev40-shard-2" for n in ("011", "012", "013", "014")})
    for job in allocation["jobs"]:
        selection.update({n: "continuation-" + str(job["id"]) for n in job["cases"]})
    if set(selection) != set(cases) or len(selection) != 40:
        raise ValueError("not_exactly_forty_unique_pairs")
    batches, rows = {}, []
    for number, suffix in sorted(selection.items()):
        folder = ROOT / "var/validation" / (PREFIX + suffix)
        if suffix not in batches:
            prereg = read(folder / "preregistration.json")
            snapshot = folder / "frozen-sources"
            source = snapshot if snapshot.exists() else ROOT
            if any(sha(source / p) != v for p, v in prereg["sources"].items()):
                raise ValueError("batch_source_changed:" + suffix)
            if (
                sha(ROOT / "evaluations/rag_dev40_userreview_v1/evaluation.json")
                != prereg["dataset_sha256"]
            ):
                raise ValueError("question_set_changed")
            summary = read(folder / "summary.json")
            if not summary["inputs_unchanged"]:
                raise ValueError("batch_inputs_changed")
            config = load_config(source / prereg["config_path"])
            for path, digest in allocation["source_runtime"].items():
                if prereg["sources"][path] != digest:
                    raise ValueError("runtime_versions_cannot_be_pooled")
            batches[suffix] = (summary, config, load_corpus(config))
        summary, config, records = batches[suffix]
        pair = next(p for p in summary["cases"] if p["case_id"][-3:] == number)
        for mode in ("fast", "deep"):
            report = read(folder / (number + "-" + mode) / "result.json")
            check = verify_result(report, records, config)
            cited = {
                e["reference"]["chunk_id"]
                for claim in report.get("result", {}).get("claims", [])
                for e in claim["evidence"]
            }
            coverage = sum(
                any(set(bundle) <= cited for bundle in group["alternatives"])
                for group in labels[number]
            ) / len(labels[number])
            if abs(coverage - pair["modes"][mode]["cited_evidence_group_coverage"]) > 1e-12:
                raise ValueError("cited_group_coverage_mismatch")
            review = pair["review"].get("by_mode", {}).get(mode, {})
            if review.get("verdict") == "pass" and (
                set(review["covered_gold_ids"])
                != {f"G{i}" for i in range(len(cases[number]["required_claims"]))}
                or review["unsupported_claims"]
                or review["numeric_or_entity_errors"]
                or report["status"] != "passed"
            ):
                raise ValueError("inconsistent_model_review_pass")
            rows.append(
                {
                    "case_id": cases[number]["case_id"],
                    "category": cases[number]["category"],
                    "batch": folder.name,
                    "mode": mode,
                    "execution": report["status"],
                    "provenance": check,
                    "model_review": review.get("verdict", "unavailable"),
                    "review_error_type": pair["review"].get("error_type"),
                    "unsupported_claim_count": len(review.get("unsupported_claims", [])),
                    "numeric_or_entity_error_count": len(
                        review.get("numeric_or_entity_errors", [])
                    ),
                    "covered_gold_count": len(review.get("covered_gold_ids", [])),
                    "required_gold_count": len(cases[number]["required_claims"]),
                    "seconds": report["elapsed_seconds"],
                    "cited_group_coverage": coverage,
                    "budget": {
                        k: report["budget"][k]
                        for k in (
                            "llm_calls",
                            "charged_tokens",
                            "provider_input_tokens",
                            "provider_output_tokens",
                            "provider_cached_tokens",
                            "exhausted",
                        )
                    },
                }
            )

    def aggregate(subset):
        return {
            "runs": len(subset),
            "execution_passed": sum(r["execution"] == "passed" for r in subset),
            "provenance_verified": sum(r["provenance"]["execution_passed"] for r in subset),
            "model_review_passed": sum(r["model_review"] == "pass" for r in subset),
            "model_review_failed": sum(r["model_review"] == "fail" for r in subset),
            "model_review_unavailable": sum(r["model_review"] == "unavailable" for r in subset),
            "median_seconds": statistics.median(r["seconds"] for r in subset),
            "max_seconds": max(r["seconds"] for r in subset),
            "mean_cited_group_coverage": statistics.fmean(
                r["cited_group_coverage"] for r in subset
            ),
            "model_requests": sum(r["budget"]["llm_calls"] for r in subset),
        }

    result = {
        "scope": "development-only-not-independent-admission",
        "paired_cases": 40,
        "runtime_sha256": allocation["source_runtime"],
        "rows": rows,
        "aggregate": {m: aggregate([r for r in rows if r["mode"] == m]) for m in ("fast", "deep")},
        "by_category": {
            c: {
                m: aggregate([r for r in rows if r["category"] == c and r["mode"] == m])
                for m in ("fast", "deep")
            }
            for c in sorted({r["category"] for r in rows})
        },
        "batch_usage_including_retained_partial_attempts": {
            k: v[0]["budget"] for k, v in batches.items()
        },
        "judge_scope": "same-provider-fresh-context-not-human-independent",
        "failures_retained": True,
        "default_production_changed": False,
        "user_latency_accepted": True,
        "independent_admission": False,
    }
    path = ROOT / "docs/validation/20261026-rag-06b-qa.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"aggregate": result["aggregate"], "by_category": result["by_category"]}))


if __name__ == "__main__":
    main()
