"""Report a source subtotal/reference conflict without changing frozen labels or scores."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    config = json.loads((ROOT / "services/rag/qwen3-06b-dev40-v1.json").read_text())
    entry = config["inputs"]["chunks"]
    corpus = Path(entry["path"]).read_bytes()
    if hashlib.sha256(corpus).hexdigest() != entry["sha256"]:
        raise ValueError("corpus_changed")
    rows = [json.loads(line) for line in corpus.decode().splitlines()]
    tables = {}
    pattern = re.compile(r"^\|\s*`(scat\d+_\d+\.zip)`\s*\|[^\n]+\|\s*([\d,]+)\s*\|$", re.MULTILINE)
    for tag in ("Table 1A：", "Table 1B："):
        matches = [
            r
            for r in rows
            if r["source_id"] == "scat-dataset-curated"
            and tag in " > ".join(r.get("section_path", []))
        ]
        if len(matches) != 1:
            raise ValueError("ambiguous_table_chunk")
        row = matches[0]
        packages = [
            {"package": name, "flights": int(count.replace(",", ""))}
            for name, count in pattern.findall(row["text"])
        ]
        tables[tag] = {
            "chunk_id": row["chunk_id"],
            "source_version": row["source_version"],
            "text_sha256": row["text_sha256"],
            "packages": packages,
            "computed_subtotal": sum(x["flights"] for x in packages),
        }
    a, b = tables["Table 1A："], tables["Table 1B："]
    if (
        len(a["packages"]) != 6
        or len(b["packages"]) != 7
        or a["computed_subtotal"] + b["computed_subtotal"] != 167547
        or b["computed_subtotal"] != 94992
    ):
        raise ValueError("arithmetic_assumption_not_verified")
    report = {
        "status": "verified-source-and-reference-scope-conflict",
        "case_id": "DEV-20261025-dev40-040",
        "corpus_sha256": entry["sha256"],
        "tables": tables,
        "all_13_packages_total": 167547,
        "table_1b_7_packages_subtotal": 94992,
        "printed_table_1b_total_and_frozen_reference": 167547,
        "conclusion": "The printed total/reference is for all 13 packages, whereas this question asks for Table 1B's seven packages. The deep answer detected a real source scope inconsistency; raw judge failure is retained and must not be treated as an established model arithmetic error.",
        "proposed_source_revision": "Label Table 1B subtotal 94,992; label 167,547 separately as Table 1A+1B total for all 13 packages.",
        "proposed_reference_revision": "Keep dates and 14,365 for the named package; answer seven-package subtotal 94,992 and explicitly distinguish the all-package total 167,547.",
        "original_labels_and_scores_modified": False,
        "automatic_dataset_rewrite": False,
        "requires_new_reviewed_version_before_rescoring": True,
        "external_model_requests": 0,
    }
    (ROOT / "docs/validation/20261026-rag-06b-source-conflict.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "status",
                    "table_1b_7_packages_subtotal",
                    "all_13_packages_total",
                    "original_labels_and_scores_modified",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
