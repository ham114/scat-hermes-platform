"""Source-shaped regressions: semantic boundaries, geometry and no truncation."""
import copy

import pytest

from rag_retrieval.table_chunks import audit_cell_coverage, cell_grid, descendant_text, isolate_conflicting_rows, serialize_table


def cell(row, col, text, *, rows=1, cols=1, header=False):
    return {"start_row_offset_idx": row, "end_row_offset_idx": row + rows,
            "start_col_offset_idx": col, "end_col_offset_idx": col + cols,
            "row_span": rows, "col_span": cols, "text": text, "column_header": header}


def table(rows):
    return {"num_rows": len(rows), "num_cols": len(rows[0]), "table_cells": [
        cell(r, c, text, header=r == 0) for r, row in enumerate(rows) for c, text in enumerate(row)
    ]}


def test_precipitation_threshold_retains_parent_and_class_columns():
    data = table([["Measurand", "Class A", "Class B"],
                  ["Liquid precipitation intensity", "Greater of 0.2 mm/h or 5%", "Greater of 0.5 mm/h or 10%"],
                  ["Starting threshold", "0.1 mm/h", "0.3 mm/h"]])
    chunks = serialize_table(data, "WMO Table 1", len, row_context={2: "Parent measurand: Liquid precipitation intensity"}, target_tokens=80, max_tokens=600)
    hit = next(c for c in chunks if "0.1 mm/h" in c["text"])
    assert "Parent measurand: Liquid precipitation intensity" in hit["text"]
    assert "C2[Class A] = 0.1 mm/h" in hit["text"]
    assert "C3[Class B] = 0.3 mm/h" in hit["text"]
    assert "Greater of 0.2" not in hit["text"]
    assert audit_cell_coverage(data, chunks) == 9


def test_bit_value_rows_repeat_explicit_spanning_stub():
    data = {"num_rows": 2, "num_cols": 3, "table_cells": [
        cell(0, 0, "bit-8 (NAV)", rows=2), cell(0, 1, "= 0"), cell(0, 2, "available"),
        cell(1, 1, "= 1"), cell(1, 2, "not available")]}
    chunks = serialize_table(data, "I062/380 Subfield #8", len, target_tokens=50, max_tokens=250)
    for chunk in chunks:
        if "= 0" in chunk["text"]:
            assert "bit-8 (NAV)" in chunk["text"] and "C3 = available" in chunk["text"]
        if "= 1" in chunk["text"]:
            assert "bit-8 (NAV)" in chunk["text"] and "C3 = not available" in chunk["text"]
    assert audit_cell_coverage(data, chunks) == 5


def test_blank_cells_are_not_implicitly_forward_filled():
    data = table([["Field", "Value"], ["first", "a"], ["", "b"]])
    assert cell_grid(data)[2][0]["text"] == ""


def test_oversized_cell_retains_exact_character_intervals():
    text = "long-cell-value;" * 200
    data = table([["Description"], [text]])
    chunks = serialize_table(data, "Table", len, target_tokens=170, max_tokens=180)
    assert len(chunks) > 10
    assert all(len(c["text"]) <= 180 for c in chunks)
    assert audit_cell_coverage(data, chunks) == 2
    pieces = [text[s["start_char"]:s["end_char"]] for c in chunks for s in c["cell_segments"] if s["cell_index"] == 1]
    assert "".join(pieces) == text


def test_conflicting_geometry_is_rejected_without_guessing():
    data = table([["A", "B"]])
    data["table_cells"].append(cell(0, 0, "conflicting"))
    with pytest.raises(ValueError, match="overlapping_table_cells"):
        serialize_table(data, "Table", len)


def test_coverage_audit_detects_a_lost_cell():
    data = table([["A", "B"], ["1", "2"]])
    chunks = serialize_table(data, "Table", len)
    damaged = copy.deepcopy(chunks)
    for c in damaged:
        c["cell_segments"] = [s for s in c["cell_segments"] if s["cell_index"] != 3]
    with pytest.raises(ValueError, match="table_cell_text_missing"):
        audit_cell_coverage(data, damaged)


def test_conflicting_diagram_row_does_not_discard_sound_altitude_definition():
    data = table([["bad diagram", "8"], ["bits-13/1", "Altitude LSB=25ft -1300ft <= Altitude <= 100000ft"]])
    data["table_cells"].append(cell(0, 0, "overlap"))
    safe, bad_rows, removed = isolate_conflicting_rows(data)
    assert bad_rows == [0] and len(removed) == 3
    chunks = serialize_table(safe, "CAT062", len)
    assert "bad diagram" not in chunks[0]["text"]
    assert "LSB=25ft" in chunks[0]["text"]
    assert audit_cell_coverage(safe, chunks) == 2


def test_nested_note_is_not_lost_at_an_empty_list_parent():
    doc = {"texts": [{"text": "", "children": [{"$ref": "#/groups/0"}]},
                     {"text": 'If the letter "G" is used, specify augmentation after NAV/.', "children": []}],
           "groups": [{"children": [{"$ref": "#/texts/1"}]}]}
    texts, refs = descendant_text(doc, "#/texts/0")
    assert "NAV/" in "".join(texts)
    assert refs == ["#/texts/0", "#/groups/0", "#/texts/1"]
