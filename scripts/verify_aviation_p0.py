"""Offline structural acceptance and optional local-only retrieval A/B checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rag_retrieval.table_chunks import audit_cell_coverage, cell_grid, descendant_text

TARGET = ROOT / "var/rag/corpora/kg_20261124_aviation_v6_p0"
PARENT = ROOT / "var/rag/corpora/kg_20261123_aviation_v5"
OUT = ROOT / "output/rag-p0-repair-20261124"
PROBES = [
    ("CAT-NAV", "EUROCONTROL CAT062 I062/380 Subfield #8 Trajectory Intent Status bit-8 NAV bit-7 NVB available valid", "#/tables/99", "eurocontrol-asterix-cat062", None),
    ("WMO-THRESHOLD", "WMO measurement quality classification liquid precipitation intensity starting threshold Class A Class B", "#/tables/2", "wmo-no8-volume1", 9),
    ("IFH-COLD", "FAA Instrument Flying Handbook ICAO Cold Temperature Error Table reported temperature -20 height above airport 1000 1500 feet", "#/tables/0", "faa-instrument-flying-handbook", 5),
    ("PHAK-VSI", "FAA PHAK when alternate static source pressure is used VSI momentary climb altitude held constant", None, "faa-phak", None),
    ("AIM-GNSS", "FAA flight plan Item 10a letter G GNSS Item 18 NAV external augmentation", None, "faa-aim", None),
    ("CAT-ALTITUDE", "CAT062 I062/380 Final State Selected Altitude two complement LSB 25 feet range", "#/tables/97", "eurocontrol-asterix-cat062", 6),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")


def structural():
    summary = read(TARGET / "build-summary.json")
    assert sha(TARGET / "rag_chunks.v1.jsonl") == summary["corpus_sha256"]
    assert sha(PARENT / "rag_chunks.v1.jsonl") == summary["parent_sha256"]
    old = (PARENT / "rag_chunks.v1.jsonl").read_bytes().splitlines(keepends=True)
    raw = (TARGET / "rag_chunks.v1.jsonl").read_bytes()
    assert raw.startswith(b"".join(old[:149]))
    rows = {r["chunk_id"]: r for r in map(json.loads, raw.decode().splitlines())}
    assert len(rows) == summary["chunks"]
    tables = read(TARGET / "table-audit.json")
    checked, excluded = 0, 0
    for table in tables["tables"]:
        p = ROOT / table["artifact"]
        artifact = read(p)
        original = artifact["original_table"]["data"]["table_cells"]
        data = artifact["normalized_data"]
        valid = data["table_cells"]
        present = {c["original_cell_index"] for c in valid}
        removed = set(artifact["quarantined_original_cell_indices"])
        assert present.isdisjoint(removed) and present | removed == set(range(len(original)))
        for c in valid:
            assert c["text"] == original[c["original_cell_index"]]["text"]
        excluded += len(removed)
        cell_grid(data)
        chunks = []
        for cid in table["chunks"]:
            row = rows[cid]
            assert row["locator"]["structured_table_sha256"] == sha(p)
            chunks.append({"text": row["text"], "cell_segments": row["locator"]["cell_segments"]})
        checked += audit_cell_coverage(data, chunks)
    assert checked == summary["nonempty_cells_checked"]
    for row in rows.values():
        assert hashlib.sha256(row["text"].encode()).hexdigest() == row["text_sha256"]
        assert row["token_count"] <= 1800
    nav = next(r for r in rows.values() if r["source_id"] == "eurocontrol-asterix-cat062" and r["locator"].get("doc_item_refs") == ["#/tables/99"])
    for a, b in [("= 0", "is available"), ("= 1", "is not available"), ("= 0", "is valid"), ("= 1", "is not valid")]:
        assert any(a in line and b in line for line in nav["text"].splitlines())
    wmo = next(r for r in rows.values() if "Parent measurand: Liquid precipitation intensity" in r["text"])
    assert "C2[Class A] = 0.1 mm/h" in wmo["text"] and "C3[Class B] = 0.3 mm/h" in wmo["text"]
    assert any('When the alternate static source pressure is used' in r["text"] and 'The VSI shows a momentary climb' in r["text"] for r in rows.values())
    assert any('Original HTML paired paragraphs, Item 10a: G = GNSS' in r["text"] for r in rows.values())
    assert any('If the letter "G" is used, the types of external GNSS augmentation' in r["text"] and 'NAV/' in r["text"] and r["chunk_type"] == "text" for r in rows.values())
    assert any(r["source_id"] == "eurocontrol-asterix-cat062" and r["locator"].get("doc_item_refs") == ["#/tables/97"] and "LSB=25ft" in r["text"] for r in rows.values())
    result = {"status": "passed", "chunks": len(rows), "tables": len(tables["tables"]), "nonempty_cells_checked": checked,
              "quarantined_original_cells": excluded, "whole_empty_tables": summary["quarantined_tables"],
              "partially_quarantined_tables": summary["partially_quarantined_tables"], "original_149_bytes_unchanged": True,
              "known_source_regressions": [p[0] for p in PROBES], "external_model_calls": 0}
    save(OUT / "structural-audit.json", result)
    return rows, result


def retrieval(rows):
    from verify_aviation_retrieval import PROBES as SOURCE_PROBES
    opener = build_opener(ProxyHandler({}))
    digest = sha(TARGET / "rag_chunks.v1.jsonl")

    def query(port, qid, text):
        path = OUT / "retrieval" / str(port) / (qid + ".json")
        if path.exists():
            result = read(path)
            assert result["query"] == text
            return result
        started = time.monotonic()
        req = Request(f"http://127.0.0.1:{port}/search", data=json.dumps({"query": text, "limit": 10}).encode(), headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=90) as response:
            payload = json.load(response)
        expected = digest if port == 18134 else sha(PARENT / "rag_chunks.v1.jsonl")
        assert payload["corpus_sha256"] == expected
        result = {"query": text, "corpus_sha256": expected, "results": payload["results"], "seconds": time.monotonic() - started}
        save(path, result)
        return result

    save(OUT / "retrieval-preregistration.json", {"p0_probes": PROBES, "source_probes": SOURCE_PROBES,
         "old_single_turn_cases": 113, "model_parameters_changed": False, "external_model_calls": 0})
    probes = []
    for qid, text, ref, source, row_index in PROBES:
        value = query(18134, qid, text)
        hits = []
        for rank, r in enumerate(value["results"], 1):
            if r["source_id"] != source:
                continue
            if ref and ref not in r["locator"].get("doc_item_refs", []):
                continue
            if row_index is not None and row_index not in r["locator"].get("row_indices", []):
                continue
            if qid == "PHAK-VSI" and not ('When the alternate static source pressure is used' in r["text"] and 'momentary climb' in r["text"]):
                continue
            if qid == "AIM-GNSS" and not ('If the letter "G" is used' in r["text"] and 'NAV/' in r["text"]):
                continue
            hits.append(rank)
        probes.append({"case_id": qid, "evidence_rank": min(hits) if hits else None, "passed_at_10": bool(hits)})
    smoke = []
    for qid, source, text in SOURCE_PROBES:
        value = query(18134, qid, text)
        smoke.append({"case_id": qid, "passed": any(r["source_id"] == source for r in value["results"])})
    comparisons = []
    for filename in ["evaluation.json", "regression.json"]:
        for case in read(ROOT / "evaluations/rag_p0_reviewed_20261123_v1" / filename)["cases"]:
            if case.get("turns"):
                continue
            cid = case["case_id"]
            metrics = {}
            for port in [18133, 18134]:
                value = query(port, cid, case["question"])
                ids = [r["chunk_id"] for r in value["results"]]
                for k in [5, 10]:
                    metrics[f"{port}_complete_at_{k}"] = all(set(g) <= set(ids[:k]) for g in case["supporting_evidence_groups"])
            comparisons.append({"case_id": cid, **metrics})
            print(json.dumps({"retrieval_regression": len(comparisons), "case": cid}), flush=True)
    losses = {str(k): [c["case_id"] for c in comparisons if c[f"18133_complete_at_{k}"] and not c[f"18134_complete_at_{k}"]] for k in [5, 10]}
    result = {"status": "passed" if all(p["passed_at_10"] for p in probes) and all(s["passed"] for s in smoke) and not losses["5"] and not losses["10"] else "needs-review",
              "p0_probes": probes, "source_smoke_passed": sum(s["passed"] for s in smoke), "source_smoke_total": len(smoke),
              "old_regressions": len(comparisons), "losses": losses,
              "complete_counts": {f"{port}_at_{k}": sum(c[f"{port}_complete_at_{k}"] for c in comparisons) for port in [18133, 18134] for k in [5, 10]},
              "comparisons": comparisons, "external_model_calls": 0, "qa_rerun": False}
    save(OUT / "retrieval-summary.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval", action="store_true")
    args = parser.parse_args()
    rows, result = structural()
    print(json.dumps(result), flush=True)
    if args.retrieval:
        retrieval(rows)
