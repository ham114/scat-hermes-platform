"""Lossless, position-aware Docling table serialization for retrieval.

Never infer a column heading from a column number, or carry a blank cell
forward. Explicit row/column spans are expanded; reviewed exceptions belong
in a versioned source overlay, not in this generic serializer.
"""

from __future__ import annotations

import json
import copy
from collections.abc import Callable


def cell_grid(data: dict) -> list[list[dict | None]]:
    grid = [[None for _ in range(data["num_cols"])] for _ in range(data["num_rows"])]
    for index, cell in enumerate(data["table_cells"]):
        r0, r1 = cell["start_row_offset_idx"], cell["end_row_offset_idx"]
        c0, c1 = cell["start_col_offset_idx"], cell["end_col_offset_idx"]
        if not (0 <= r0 < r1 <= len(grid) and 0 <= c0 < c1 <= data["num_cols"]):
            raise ValueError("invalid_table_cell_span")
        for row in range(r0, r1):
            for col in range(c0, c1):
                if grid[row][col] is not None:
                    raise ValueError("overlapping_table_cells")
                grid[row][col] = {**cell, "cell_index": index}
    return grid


def isolate_conflicting_rows(data: dict) -> tuple[dict, list[int], list[int]]:
    """Quarantine intersecting rows, retaining independently sound rows.

    Expand to the entire span of every affected cell. Never clip a merged
    cell to manufacture a new relationship. Original indices remain traceable.
    """
    occupied = {}
    bad_rows = set()
    cells = data["table_cells"]
    for i, cell in enumerate(cells):
        r0, r1 = cell["start_row_offset_idx"], cell["end_row_offset_idx"]
        c0, c1 = cell["start_col_offset_idx"], cell["end_col_offset_idx"]
        if not (0 <= r0 < r1 <= data["num_rows"] and 0 <= c0 < c1 <= data["num_cols"]):
            raise ValueError("invalid_table_cell_span")
        for r in range(r0, r1):
            for c in range(c0, c1):
                if (r, c) in occupied:
                    bad_rows.add(r)
                occupied[r, c] = i
    while True:
        expanded = set(bad_rows)
        for cell in cells:
            rows = set(range(cell["start_row_offset_idx"], cell["end_row_offset_idx"]))
            if rows & bad_rows:
                expanded |= rows
        if expanded == bad_rows:
            break
        bad_rows = expanded
    result = copy.deepcopy(data)
    removed = []
    kept = []
    for i, cell in enumerate(result["table_cells"]):
        if set(range(cell["start_row_offset_idx"], cell["end_row_offset_idx"])) & bad_rows:
            removed.append(i)
        else:
            kept.append({**cell, "original_cell_index": i})
    result["table_cells"] = kept
    cell_grid(result)
    return result, sorted(bad_rows), removed


def descendant_text(doc: dict, ref: str, stack: tuple = ()) -> tuple[list[str], list[str]]:
    """Read text in nested Docling lists as well as their empty parents."""
    if ref in stack:
        raise ValueError("docling_reference_cycle")
    name, index = ref[2:].split("/")
    item = doc[name][int(index)]
    texts, refs = [], [ref]
    value = item.get("text") or item.get("orig") or ""
    if value:
        texts.append(value)
    for child in item.get("children", []):
        child_text, child_refs = descendant_text(doc, child["$ref"], stack + (ref,))
        texts.extend(child_text)
        refs.extend(child_refs)
    return texts, refs


def serialize_table(
    data: dict,
    prefix: str,
    count_tokens: Callable[[str], int],
    *,
    row_context: dict[int, str] | None = None,
    target_tokens: int = 640,
    max_tokens: int = 1800,
) -> list[dict]:
    """Repeat headers and row stubs; retain every cell and its source offsets.

    Normal rows are atomic. Very long rows split at cell boundaries; an
    oversized cell alone splits by exact character intervals. No content is
    dropped. A returned segment points to the *original* cell, even when a
    spanning cell is repeated. Empty cells remain in the structured sidecar.
    """
    grid = cell_grid(data)
    row_context = row_context or {}
    headers: dict[int, list[str]] = {}
    header_rows = set()
    for ri, row in enumerate(grid):
        present = [c for c in row if c and c["text"].strip()]
        if not present or sum(bool(c.get("column_header")) for c in present) < len(present) / 2:
            break
        header_rows.add(ri)
        for ci, cell in enumerate(row):
            if cell and cell.get("column_header") and cell["text"].strip():
                values = headers.setdefault(ci, [])
                if cell["text"] not in values:
                    values.append(cell["text"])

    def label(col: int) -> str:
        value = " / ".join(headers.get(col, []))
        # Full headers are themselves serialized below, never truncated.
        return f"C{col + 1}[{value}]" if value and len(value) <= 100 else f"C{col + 1}"

    units = []
    for ri, row in enumerate(grid):
        # Repeat explicit row identifiers on split rows. Do not promote an
        # arbitrary continuation row into the previous row's semantic scope.
        keys = []
        if ri not in header_rows:
            for ci, cell in enumerate(row[:2]):
                if cell and cell["text"].strip() and len(cell["text"]) <= 100:
                    keys.append(f"{label(ci)}={json.dumps(cell['text'], ensure_ascii=False)}")
        lead = f"Row {ri + 1}"
        if keys:
            lead += "; row identifiers: " + ", ".join(keys)
        if ri in row_context:
            lead += "; " + row_context[ri]
        lead += " | "
        entries = []
        seen = set()
        for ci, cell in enumerate(row):
            if not cell or not cell["text"] or cell["cell_index"] in seen:
                continue
            seen.add(cell["cell_index"])
            text = cell["text"]
            c0, c1 = cell["start_col_offset_idx"], cell["end_col_offset_idx"]
            tag = label(c0) if c1 == c0 + 1 else f"C{c0 + 1}..C{c1}"
            entries.append((tag, text, cell["cell_index"]))
        if not entries:
            continue

        def render(parts):
            return lead + " | ".join(f"{tag} = {value}" for tag, value, _, _, _ in parts)

        whole = [(tag, value, idx, 0, len(value)) for tag, value, idx in entries]
        if count_tokens(prefix + "\n" + render(whole)) <= max_tokens:
            units.append({"text": render(whole), "row": ri, "segments": whole})
            continue
        part = []
        for tag, value, idx in entries:
            start = 0
            while start < len(value):
                candidate = (tag, value[start:], idx, start, len(value))
                if count_tokens(prefix + "\n" + render(part + [candidate])) <= target_tokens:
                    part.append(candidate)
                    break
                if part:
                    units.append({"text": render(part), "row": ri, "segments": part})
                    part = []
                    continue
                lo, hi = start + 1, len(value)
                best = start
                while lo <= hi:
                    mid = (lo + hi) // 2
                    seg = (tag, value[start:mid], idx, start, mid)
                    if count_tokens(prefix + "\n" + render([seg])) <= target_tokens:
                        best, lo = mid, mid + 1
                    else:
                        hi = mid - 1
                if best == start:
                    raise ValueError("table_metadata_exceeds_token_budget")
                seg = (tag, value[start:best], idx, start, best)
                units.append({"text": render([seg]), "row": ri, "segments": [seg]})
                start = best
        if part:
            units.append({"text": render(part), "row": ri, "segments": part})

    chunks = []
    current = []

    def flush():
        if not current:
            return
        text = prefix + "\n" + "\n".join(u["text"] for u in current)
        chunks.append({
            "text": text,
            "row_indices": [u["row"] for u in current],
            "cell_segments": [
                {"cell_index": i, "start_char": s, "end_char": e, "row": u["row"]}
                for u in current for _, _, i, s, e in u["segments"]
            ],
            "token_count": count_tokens(text),
        })
        current.clear()

    for unit in units:
        if current and count_tokens(prefix + "\n" + "\n".join(
            u["text"] for u in current + [unit]
        )) > target_tokens:
            flush()
        current.append(unit)
    flush()
    if any(c["token_count"] > max_tokens for c in chunks):
        raise ValueError("table_chunk_exceeds_token_budget")
    return chunks


def audit_cell_coverage(data: dict, chunks: list[dict]) -> int:
    """Independently verify exact coverage and publication of every cell value."""
    covered: dict[int, list[tuple[int, int]]] = {}
    for chunk in chunks:
        for seg in chunk["cell_segments"]:
            i, a, b = seg["cell_index"], seg["start_char"], seg["end_char"]
            value = data["table_cells"][i]["text"]
            if not (0 <= a < b <= len(value)) or value[a:b] not in chunk["text"]:
                raise ValueError("table_cell_segment_not_published")
            covered.setdefault(i, []).append((a, b))
    for i, cell in enumerate(data["table_cells"]):
        if not cell["text"]:
            continue
        end = 0
        for a, b in sorted(covered.get(i, [])):
            if a > end:
                raise ValueError("table_cell_text_gap")
            end = max(end, b)
        if end != len(cell["text"]):
            raise ValueError("table_cell_text_missing")
    return len(covered)
