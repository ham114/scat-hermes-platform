"""Freeze the reviewed development dataset before retrieving any rankings."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rag_retrieval.reviewed_dataset import convert_reviewed

SOURCE = Path(
    "/home/h3c/project/python/ragas-evaluation/reports/batch-20261025-dev40/user-review-20261026/candidates.jsonl"
)
EXPECTED = "f24561457bd8876d0e8e65a0adb6962306fd048bad5ae8d4df3461bf0be4add9"
RULES = ROOT / "services/rag/reviewed-dev40-label-rules-v1.json"
DESTINATION = ROOT / "evaluations/rag_dev40_userreview_v1"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if Path(sys.prefix).resolve() != Path("/home/h3c/anaconda3/envs/scat-agent"):
        raise ValueError("use_existing_scat_agent")
    stage = ROOT / ".tmp/reviewed-rag/prepare"
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(stage)
    if DESTINATION.exists() or sha(SOURCE) != EXPECTED:
        raise ValueError("existing_dataset_or_changed_source")
    old_config = json.loads((ROOT / "services/rag/qwen3-comparison-v1.json").read_text())
    corpus_path = Path(old_config["inputs"]["chunks"]["path"])
    corpus_sha = old_config["inputs"]["chunks"]["sha256"]
    if sha(corpus_path) != corpus_sha:
        raise ValueError("corpus_changed")
    candidates = [json.loads(l) for l in SOURCE.read_text().splitlines()]
    corpus = [json.loads(l) for l in corpus_path.read_text().splitlines()]
    evaluation, claims, audit = convert_reviewed(
        candidates, corpus, json.loads(RULES.read_text()), corpus_sha
    )
    if len(candidates) != 40 or len(corpus) != 149:
        raise ValueError("unexpected_case_or_corpus_count")
    DESTINATION.mkdir()
    save(DESTINATION / "evaluation.json", evaluation)
    save(DESTINATION / "claim_groups.json", claims)
    save(DESTINATION / "label-audit.json", audit)
    manifest = {
        "scope": evaluation["scope"],
        "dataset_version": evaluation["dataset_version"],
        "claim_group_version": claims["dataset_version"],
        "case_count": 40,
        "source_path": str(SOURCE),
        "source_sha256": EXPECTED,
        "corpus_sha256": corpus_sha,
        "rules_sha256": sha(RULES),
        "converted_question_answer_changes": 0,
        "source_code_sha256": {
            p: sha(ROOT / p)
            for p in [
                "scripts/prepare_reviewed_rag_dataset.py",
                "src/rag_retrieval/reviewed_dataset.py",
            ]
        },
        "files": {
            name: sha(DESTINATION / name)
            for name in ["evaluation.json", "claim_groups.json", "label-audit.json"]
        },
        "freeze_scope": "development-regression-only",
        "rankings_seen_during_labeling": False,
        "qa_started": False,
        "external_api_calls": 0,
    }
    save(DESTINATION / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "state": "frozen-development-version",
                "manifest": manifest,
                "groups": audit["groups"],
                "excluded_claims": audit["excluded_claims"],
                "single_chunk_multi_source_cases": [
                    c["case_id"]
                    for c in audit["cases"]
                    if c["authored_multi_source_but_single_chunk_sufficient"]
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
