"""Build a versioned P0 corpus from frozen Docling cells, without model APIs.

Run in the existing server docling environment. Old source/extraction files,
v5 chunks, evaluations and scores are read-only inputs. Production switching
is a separate, guarded deployment step.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rag_retrieval.table_chunks import audit_cell_coverage, descendant_text, isolate_conflicting_rows, serialize_table

PARENT = ROOT / "var/rag/corpora/kg_20261123_aviation_v5"
TARGET = ROOT / "var/rag/corpora/kg_20261124_aviation_v6_p0"
EXPECTED = "ef2a612511771646e54108eb9ee3c886195fd36d2c5f74963953704ee4defb4f"
MODEL = "/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181"
EXTRACT = "var/rag/sources/official-aviation-20261123/extracted/"
CAT = EXTRACT + "eurocontrol-asterix-cat062/document.rag.json"
WMO = EXTRACT + "wmo-no8-volume1/WMO-8-vI-2024_en-p0099-0200/document.rag.json"
IFH = EXTRACT + "faa-instrument-flying-handbook/faa-h-8083-15b-p0099-0200/document.rag.json"
PHAK = EXTRACT + "faa-phak/faa-h-8083-25c-p0199-0300/document.rag.json"
AIM_APP4 = EXTRACT + "faa-aim/aim-appendix_4/document.rag.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def source_overlay(document, ref, table):
    """Reviewed layout relationships only; every original cell value retained."""
    data = copy.deepcopy(table["data"])
    contexts, changes = {}, []
    cells = data["table_cells"]
    if document == CAT and ref == "#/tables/99":
        expected = [(0, "bit-8", "(NAV)"), (2, "bit-7", "(NVB)"), (5, "bit-1", "(FX)")]
        for row, bit, name in expected:
            for col, text in [(0, bit), (1, name)]:
                cell = next(c for c in cells if c["start_row_offset_idx"] == row and c["start_col_offset_idx"] == col)
                assert cell["text"] == text and cell["row_span"] == 1
                assert not any(c["start_row_offset_idx"] == row + 1 and c["start_col_offset_idx"] == col for c in cells)
                cell.update(row_span=2, end_row_offset_idx=row + 2)
            changes.append({"row": row, "columns": [0, 1], "operation": "extend_stub_to_next_value_row", "pdf_page": 98})
    elif document == WMO and ref == "#/tables/2":
        for row, parent in [(5, "Wind speed and gust"), (9, "Liquid precipitation intensity")]:
            assert next(c["text"] for c in cells if c["start_row_offset_idx"] == row and c["start_col_offset_idx"] == 0) == "Starting threshold"
            assert next(c["text"] for c in cells if c["start_row_offset_idx"] == row - 1 and c["start_col_offset_idx"] == 0) == parent
            contexts[row] = "Parent measurand: " + parent
            changes.append({"row": row, "operation": "repeat_reviewed_parent_measurand", "value": parent, "pdf_page": 104})
    elif document == IFH and ref == "#/tables/0":
        for cell in cells:
            if cell["start_row_offset_idx"] == 1:
                cell["column_header"] = True
        for row in range(2, data["num_rows"]):
            temperature = next(c["text"] for c in cells if c["start_row_offset_idx"] == row and c["start_col_offset_idx"] == 1)
            contexts[row] = f"Reported temperature: {temperature} °C; cell values are altitude corrections in feet"
        changes.append({"operation": "restore_second_header_row_and_temperature_axis", "pdf_page": 101})
    elif document == AIM_APP4 and ref == "#/tables/1":
        from bs4 import BeautifulSoup
        html = ROOT / "var/rag/sources/official-aviation-20261123/originals/aim-appendix_4.html"
        soup = BeautifulSoup(html.read_bytes(), "html.parser")
        native = next(t for t in soup.find_all("table") if "GNSS" in t.get_text() and "LPV" in t.get_text())
        columns = native.find_all("td")
        codes = [p.get_text(" ", strip=True) for p in columns[0].find_all("p", recursive=False)]
        meanings = [p.get_text(" ", strip=True) for p in columns[1].find_all("p", recursive=False)]
        assert len(codes) == len(meanings) and codes[8] == "G" and meanings[8] == "GNSS (See Note 2)"
        contexts[0] = "Original HTML paired paragraphs, Item 10a: G = GNSS (See Note 2)"
        changes.append({"operation": "restore_reviewed_html_paragraph_pair", "code": "G", "meaning": meanings[8],
                        "source_html": str(html.relative_to(ROOT)), "source_html_sha256": digest(html.read_bytes()), "paragraph_index_0_based": 8})
    assert [c["text"] for c in cells] == [c["text"] for c in table["data"]["table_cells"]]
    return data, contexts, changes


def build():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    if (TARGET / "rag_chunks.v1.jsonl").exists():
        raise ValueError("immutable_v6_already_built")
    raw = (PARENT / "rag_chunks.v1.jsonl").read_bytes()
    assert digest(raw) == EXPECTED
    lines = raw.splitlines(keepends=True)
    rows = [json.loads(line) for line in lines]
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    count = lambda text: len(tokenizer.encode(text, add_special_tokens=False))
    docs, docs_hashes = {}, {}

    def document(row):
        loc = row["locator"]
        path = loc["document_json"]
        if path not in docs:
            content = (ROOT / path).read_bytes()
            docs_hashes[path] = digest(content)
            docs[path] = json.loads(content)
        assert docs_hashes[path] == loc["document_json_sha256"]
        return docs[path]

    tables = defaultdict(list)
    for row in rows:
        for ref in row.get("locator", {}).get("doc_item_refs", []):
            if ref.startswith("#/tables/"):
                tables[(row["locator"]["document_json"], ref)].append(row)
    output, mapping, table_audit, modified = [], {}, [], []

    def new_row(old, text, locator, kind):
        row = copy.deepcopy(old)
        row.update(text=text, text_sha256=digest(text.encode()), char_count=len(text), token_count=count(text), chunk_type=kind, locator=locator)
        identity = {"source_id": row["source_id"], "source_version": row["source_version"], "locator": locator, "text": text}
        row["chunk_id"] = "kco_" + digest(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode())[:32]
        row["normalization_version"] = "aviation-p0-table-cells-v1"
        assert row["token_count"] <= 1800, (row["chunk_id"], row["token_count"])
        return row

    for old in rows:
        refs = old.get("locator", {}).get("doc_item_refs", [])
        if not any(ref.startswith("#/tables/") for ref in refs):
            if old["chunk_id"] != "kco_da331635accf9991bd1ad434da9d06eb":
                output.append(old)
                mapping[old["chunk_id"]] = [old["chunk_id"]]
                continue
            doc = document(old)
            antecedent = doc["texts"][89]
            effect = doc["texts"][4657]
            assert antecedent["text"].startswith("When the alternate static source pressure is used")
            assert effect["text"] == "The VSI shows a momentary climb and then stabilizes if the altitude is held constant."
            text = old["text"].replace(effect["text"], antecedent["text"] + "\n" + effect["text"], 1)
            loc = {**old["locator"], "doc_item_refs": ["#/texts/89", "#/texts/4657", "#/texts/90"], "normalization": "restore_exact_antecedent"}
            row = new_row(old, text, loc, "text")
            output.append(row)
            mapping[old["chunk_id"]] = [row["chunk_id"]]
            modified.append({"old": old["chunk_id"], "new": row["chunk_id"], "reason": "restore_alternate_static_source_condition"})
            continue
        # Preserve prose from mixed prose/table chunks without the flawed
        # flattened table. Heading text remains source context, not a cell.
        doc = document(old)
        prose = []
        prose_refs = []
        for ref in refs:
            if ref.startswith("#/tables/"):
                continue
            values, descendants = descendant_text(doc, ref)
            prose.extend(values)
            prose_refs.extend(descendants)
        replacements = []
        if prose:
            prefix = "\n".join([f"{old['institution']} | {old['edition']} | {old['jurisdiction']}", *old["section_path"][1:]]) + "\n"
            body = "\n".join(prose)
            start = 0
            while start < len(body):
                end = len(body)
                if count(prefix + body[start:]) > 1800:
                    lo, hi, end = start + 1, len(body), start
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        if count(prefix + body[start:mid]) <= 640:
                            end, lo = mid, mid + 1
                        else:
                            hi = mid - 1
                    assert end > start
                    boundary = body.rfind("\n", start + (end - start) // 2, end)
                    if boundary > start:
                        end = boundary + 1
                text = prefix + body[start:end]
                loc = {**old["locator"], "doc_item_refs": list(dict.fromkeys(prose_refs)), "normalization": "separate_table_from_prose",
                       "source_prose_sha256": digest(body.encode()), "source_prose_charspan": [start, end]}
                row = new_row(old, text, loc, "text")
                output.append(row)
                replacements.append(row["chunk_id"])
                start = end
        mapping[old["chunk_id"]] = replacements

    for (path, ref), owners in sorted(tables.items()):
        old = owners[0]
        doc = document(old)
        table = doc["tables"][int(ref.rsplit("/", 1)[-1])]
        assert table["self_ref"] == ref
        data, contexts, changes = source_overlay(path, ref, table)
        data, excluded_rows, excluded_cells = isolate_conflicting_rows(data)
        table_id = digest((path + ref).encode())[:20]
        artifact = TARGET / "tables" / (table_id + ".json")
        payload = {"schema_version": "structured-source-table/1", "document_json": path, "document_json_sha256": docs_hashes[path], "doc_item_ref": ref,
                   "original_table": table, "normalized_data": data, "row_context": contexts, "reviewed_layout_overlays": changes,
                   "cell_text_values_changed": False, "index_base": 0,
                   "quarantined_row_indices": excluded_rows, "quarantined_original_cell_indices": excluded_cells}
        save(artifact, payload)
        captions = []
        for cap in table.get("captions", []):
            name, index = cap["$ref"][2:].split("/")
            captions.append(doc[name][int(index)].get("text", ""))
        prefix = "\n".join([f"{old['institution']} | {old['edition']} | {old['jurisdiction']}", *old["section_path"][1:], *captions,
                            f"Table {ref}; PDF pages {','.join(str(p['page_no']) for p in table['prov']) or 'HTML'}; row/column positions are 1-based."])
        if excluded_rows:
            prefix += "\n[Unavailable source rows with conflicting cell geometry: " + ",".join(str(i + 1) for i in excluded_rows) + ". Consult the original source for those rows.]"
        quarantine = "overlapping_table_rows" if excluded_rows else None
        try:
            chunks = serialize_table(data, prefix, count, row_context=contexts)
            checked = audit_cell_coverage(data, chunks)
        except ValueError as exc:
            if str(exc) not in {"overlapping_table_cells", "invalid_table_cell_span"}:
                raise
            # Invalid geometry cannot establish a safe factual mapping. Keep
            # all cells in the hashed sidecar and replace searchable content
            # with an explicit unavailable notice, never a guessed repair.
            quarantine = str(exc)
            chunks = [{"text": prefix + "\n[Table extraction unavailable: conflicting cell geometry. Consult the original source page; no factual cell mapping is asserted.]",
                       "row_indices": [], "cell_segments": []}]
            checked = 0
        if not chunks:
            quarantine = "empty_table_extraction"
            chunks = [{"text": prefix + "\n[Table extraction unavailable: no cell text extracted. Consult the original source page.]",
                       "row_indices": [], "cell_segments": []}]
        ids = []
        for index, chunk in enumerate(chunks):
            pages = sorted({p["page_no"] for p in table["prov"]})
            loc = {**old["locator"], "kind": "docling-table-cells", "doc_item_refs": [ref], "page_numbers": pages,
                   "structured_table_json": str(artifact.relative_to(ROOT)), "structured_table_sha256": digest(artifact.read_bytes()),
                   "row_indices": chunk["row_indices"], "cell_segments": chunk["cell_segments"], "chunk_index": index}
            row = new_row(old, chunk["text"], loc, "table")
            row.update(page_start=min(pages) if pages else None, page_end=max(pages) if pages else None)
            output.append(row)
            ids.append(row["chunk_id"])
        for owner in owners:
            mapping[owner["chunk_id"]].extend(ids)
        table_audit.append({"document": path, "ref": ref, "artifact": str(artifact.relative_to(ROOT)), "cells_checked": checked,
                            "chunks": ids, "overlays": changes, "source_cell_values_unchanged": True,
                            "status": ("partially_quarantined" if checked else "quarantined") if quarantine else "passed",
                            "quarantine_reason": quarantine, "quarantined_rows": excluded_rows, "quarantined_cells": excluded_cells})
        print(json.dumps({"table": len(table_audit), "of": len(tables), "chunks": len(chunks)}), flush=True)

    assert output[:149] == rows[:149]
    assert len(output) == len({r["chunk_id"] for r in output})
    mapping = {k: list(dict.fromkeys(v)) for k, v in mapping.items()}
    assert all(mapping.values())
    # Retained records keep their original JSONL bytes, not just their IDs.
    original_lines = {r["chunk_id"]: line for r, line in zip(rows, lines, strict=True)}
    corpus = b"".join(original_lines[r["chunk_id"]] if r["chunk_id"] in original_lines else
                      (json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode() for r in output)
    save(TARGET / "chunk-migration.json", mapping)
    (TARGET / "rag_chunks.v1.jsonl").write_bytes(corpus)
    save(TARGET / "table-audit.json", {"status": "passed", "table_count": len(tables), "tables": table_audit, "restored_antecedents": modified,
                                      "valid_table_nonempty_cells_published": True,
                                      "invalid_geometry_tables_quarantined": sum(t["status"] == "quarantined" for t in table_audit),
                                      "partially_quarantined_tables": sum(t["status"] == "partially_quarantined" for t in table_audit),
                                      "all_source_cells_archived": True, "document_hashes": docs_hashes, "external_model_calls": 0})
    generation = read(PARENT / "rag_generation.v1.json")
    generation.update(generation_id=TARGET.name, parent_generation_id=PARENT.name, parent_corpus_sha256=EXPECTED,
                      corpus_sha256=digest(corpus), chunk_count=len(output), status="built-pending-local-retrieval-validation",
                      normalization_version="aviation-p0-table-cells-v1", source_cell_values_changed=False,
                      chunking_config={"retained_v5_plain_text": True, "table_serializer": "rag_retrieval.table_chunks",
                                       "target_tokens": 640, "max_tokens": 1800, "long_cells": "lossless_character_intervals",
                                       "source_layout_overlays": "table-audit.json", "row_and_column_positions": True})
    save(TARGET / "rag_generation.v1.json", generation)
    save(TARGET / "build-summary.json", {"status": "passed", "parent_sha256": EXPECTED, "corpus_sha256": digest(corpus),
                                         "parent_chunks": len(rows), "chunks": len(output), "retained_chunks": sum(r["chunk_id"] in original_lines for r in output),
                                         "table_count": len(tables), "nonempty_cells_checked": sum(t["cells_checked"] for t in table_audit),
                                         "quarantined_tables": sum(t["status"] == "quarantined" for t in table_audit),
                                         "partially_quarantined_tables": sum(t["status"] == "partially_quarantined" for t in table_audit),
                                         "chunks_by_source": dict(Counter(r["source_id"] for r in output)), "external_model_calls": 0,
                                         "production_switched": False, "evaluation_scores_changed": False})
    assert digest((PARENT / "rag_chunks.v1.jsonl").read_bytes()) == EXPECTED
    print(json.dumps(read(TARGET / "build-summary.json")), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.parse_args()
    build()
