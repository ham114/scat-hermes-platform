"""Recompute real RAG provenance and development aggregates without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_result(report: dict, records: dict, config: dict):
    if report["status"] != "passed":
        return {"execution_passed": False, "error_type": report.get("error_type")}
    result = report["result"]
    if result.get("render_policy") == "claims_only_v1":
        expected_answer = (
            "\n".join(c["claim"] for c in result["claims"])
            if result["claims"]
            else "当前检索证据不足，无法确认该问题；这不代表全库不存在答案。"
        )
        if result.get("coverage") and result["status"] == "insufficient" and result["claims"]:
            gaps = [
                t["question"]
                for t, c in zip(result["targets"], result["coverage"], strict=True)
                if c["status"] != "covered"
            ]
            expected_answer = (
                "部分证据不足，以下未能确认：" + "；".join(gaps) + "。\n" + expected_answer
            )
        if result["answer"] != expected_answer:
            raise ValueError("public_answer_outside_audited_claims")
    if hashlib.sha256(result["answer"].encode()).hexdigest() != result["answer_sha256"]:
        raise ValueError("final_answer_digest_mismatch")
    reviewer = result["reviewer_session"]
    agents = report["agents"]
    deep = report["mode"] == "deep"
    if len(agents) != (4 if deep else 1) or agents[reviewer]["ordinal"] != (3 if deep else 0):
        raise ValueError("wrong_reviewer_or_session_count")
    main = next(session for session, row in agents.items() if row["ordinal"] == 0)
    from hermes_runtime.real_rag import role_tool_names

    for session, row in agents.items():
        expected = role_tool_names(
            report["mode"],
            row["ordinal"],
            planning=bool(config.get("planning_enabled")),
            scoped=bool(config.get("role_scoped_tools")),
        )
        if set(row["tools"]) != expected:
            raise ValueError("unexpected_tool_permissions")
        if session != main and row["parent_session"] != main:
            raise ValueError("parent_session_mismatch")
        if not any(
            e["event"] == "llm_request" and e.get("session") == session for e in report["events"]
        ):
            raise ValueError("session_without_real_request")
    if deep:
        if [c["count"] for c in report["delegation"]] != [2, 1]:
            raise ValueError("wrong_delegation_shape")
        spans = []
        for ordinal in (1, 2):
            session = next(s for s, row in agents.items() if row["ordinal"] == ordinal)
            start = min(
                e["time"]
                for e in report["events"]
                if e["event"] == "agent_start" and e.get("session") == session
            )
            end = max(
                e["time"]
                for e in report["events"]
                if e["event"] == "agent_end" and e.get("session") == session
            )
            spans.append((start, end))
        if max(s[0] for s in spans) >= min(s[1] for s in spans):
            raise ValueError("researchers_not_parallel")
    if config.get("planning_enabled"):
        if not result.get("targets") or len(result.get("coverage", [])) != len(result["targets"]):
            raise ValueError("missing_target_checklist")
        if not any(
            e["tool"] == "rag_plan" and e["session"] == main for e in report["evidence_operations"]
        ):
            raise ValueError("missing_actual_question_planning")
    read_ids = set()
    searched = set()
    for event in report["evidence_operations"]:
        if event["tool"] == "rag_search":
            searched.update(event["chunk_ids"])
        if event["tool"] == "rag_read" and event["session"] == reviewer:
            read_ids.update(event["evidence_ids"])
    count = 0
    for claim in result["claims"]:
        if claim["claim"] not in result["answer"]:
            raise ValueError("claim_not_in_answer")
        for citation in claim["evidence"]:
            reference = citation["reference"]
            row = records[reference["chunk_id"]]
            expected = {
                k: row[k] for k in ("chunk_id", "source_id", "source_version", "text_sha256")
            } | {
                "scope": config["scope"],
                "run_id": report["run_id"],
                "corpus_sha256": config["corpus_sha256"],
                "section_path": row.get("section_path", []),
                "locator": row.get("locator"),
            }
            if reference != expected or reference["chunk_id"] not in searched:
                raise ValueError("citation_identity_mismatch")
            identity = (
                "ev-"
                + hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()[:24]
            )
            if citation["evidence_id"] != identity or identity not in read_ids:
                raise ValueError("citation_not_personally_read")
            if (
                not citation["quote"]
                or row["text"][citation["start_char"] : citation["end_char"]] != citation["quote"]
            ):
                raise ValueError("citation_text_or_offset_mismatch")
            count += 1
    if not any(
        e["tool"] == "rag_submit"
        and e["session"] == reviewer
        and e["receipt_id"] == result["receipt_id"]
        for e in report["evidence_operations"]
    ):
        raise ValueError("missing_actual_submission")
    return {
        "execution_passed": True,
        "citations_verified": count,
        "real_native_sessions": len(agents),
        "tool_rejections": sum(e["event"] == "tool_rejected" for e in report["events"]),
    }


def main():
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from hermes_runtime.real_rag import load_config, load_corpus

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument(
        "--frozen",
        action="store_true",
        help="Verify a preserved source snapshot after subsequent development",
    )
    args = parser.parse_args()
    batch = args.batch.resolve()
    if not batch.is_relative_to((ROOT / "var/validation").resolve()):
        raise ValueError("batch_outside_project")
    source_root = batch / "frozen-sources" if args.frozen else ROOT
    summary = json.loads((batch / "summary.json").read_text())
    prereg = json.loads((batch / "preregistration.json").read_text())
    config = load_config(source_root / prereg.get("config_path", "services/rag/real-agent-v1.json"))
    records = load_corpus(config)
    if any(
        hashlib.sha256((source_root / path).read_bytes()).hexdigest() != sha
        for path, sha in prereg["sources"].items()
    ):
        raise ValueError("frozen_source_changed")
    rows, checks = [], []
    for pair in summary["cases"]:
        for mode in ("fast", "deep"):
            report = json.loads(
                (batch / (pair["case_id"][-3:] + "-" + mode) / "result.json").read_text()
            )
            check = verify_result(report, records, config)
            checks.append({"case_id": pair["case_id"], "mode": mode, **check})
            review = pair["review"].get("by_mode", {}).get(mode, {})
            rows.append(
                {
                    "case_id": pair["case_id"],
                    "mode": mode,
                    "execution": report["status"],
                    "model_review": review.get("verdict", "unavailable"),
                    "reason": review.get("reason"),
                    "seconds": report["elapsed_seconds"],
                    "model_calls": report["budget"]["llm_calls"],
                    "provider_input": report["budget"]["provider_input_tokens"],
                    "provider_output": report["budget"]["provider_output_tokens"],
                    "cited_evidence_group_coverage": pair["modes"][mode][
                        "cited_evidence_group_coverage"
                    ],
                }
            )
    aggregates = {}
    for mode in ("fast", "deep"):
        subset = [r for r in rows if r["mode"] == mode]
        aggregates[mode] = {
            "runs": len(subset),
            "execution_passed": sum(r["execution"] == "passed" for r in subset),
            "model_review_passed": sum(r["model_review"] == "pass" for r in subset),
            "median_seconds": statistics.median(r["seconds"] for r in subset),
            "mean_cited_evidence_group_coverage": statistics.mean(
                r["cited_evidence_group_coverage"] for r in subset
            ),
            "model_requests": sum(r["model_calls"] for r in subset),
        }
    result = {
        "scope": "development-only-not-independent-admission",
        "provenance_checks": checks,
        "rows": rows,
        "aggregate": aggregates,
        "batch_budget": summary["budget"],
        "production_admitted": False,
    }
    (batch / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"aggregate": aggregates, "provenance_runs": len(checks)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
