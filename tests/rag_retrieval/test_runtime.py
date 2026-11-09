from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from rag_retrieval.baseline import tokenize_unicode
from rag_retrieval.runtime import M8RRFIndex


class FakeEncoder:
    def encode(self, sentences, **kwargs):
        rows = []
        for sentence in sentences:
            text = str(sentence).casefold()
            vector = np.array(
                [float("gbar" in text), float("uav" in text), float("scat" in text)],
                dtype=np.float32,
            )
            norm = np.linalg.norm(vector)
            rows.append(vector / norm if norm else np.array([0.0, 0.0, 1.0], dtype=np.float32))
        return np.asarray(rows)


def test_runtime_returns_citable_unfinetuned_rrf_results(tmp_path: Path) -> None:
    chunks_path = tmp_path / "chunks.jsonl"
    rows = [
        {
            "chunk_id": "gbar-1",
            "source_id": "gbar-method-curated",
            "source_version": "v1",
            "section_path": ["GBAR", "input"],
            "locator": {"kind": "markdown", "start_line": 10},
            "exact_terms": ["feature_order"],
            "text": "GBAR feature_order specifies the model input order.",
        },
        {
            "chunk_id": "uav-1",
            "source_id": "uav-research-guide",
            "source_version": "v1",
            "section_path": ["UAV"],
            "locator": {"kind": "markdown", "start_line": 20},
            "exact_terms": [],
            "text": "UAV sensor channel reference.",
        },
    ]
    chunks_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    index = M8RRFIndex.from_paths(
        chunks_path=chunks_path,
        model=FakeEncoder(),
        model_id="fake-base",
        tokenizer=tokenize_unicode,
        cache_path=tmp_path / "cache.npy",
    )

    result = index.search("GBAR feature_order 是什么？", limit=1)

    assert result[0]["chunk_id"] == "gbar-1"
    assert result[0]["source_id"] == "gbar-method-curated"
    assert result[0]["locator"]["start_line"] == 10
    assert result[0]["signals"]["exact_rank"] == 1


def test_runtime_reuses_matching_embedding_cache(tmp_path: Path) -> None:
    chunks_path = tmp_path / "chunks.jsonl"
    row = {
        "chunk_id": "scat-1",
        "source_id": "scat-dataset-curated",
        "source_version": "v1",
        "section_path": [],
        "text": "SCAT dataset",
    }
    chunks_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    cache = tmp_path / "cache.npy"
    first = M8RRFIndex.from_paths(
        chunks_path=chunks_path,
        model=FakeEncoder(),
        model_id="fake-base",
        tokenizer=tokenize_unicode,
        cache_path=cache,
    )

    class QueryOnlyEncoder(FakeEncoder):
        def encode(self, sentences, **kwargs):
            assert len(sentences) == 1
            return super().encode(sentences, **kwargs)

    second = M8RRFIndex.from_paths(
        chunks_path=chunks_path,
        model=QueryOnlyEncoder(),
        model_id="fake-base",
        tokenizer=tokenize_unicode,
        cache_path=cache,
    )

    assert first.corpus_vectors.tolist() == second.corpus_vectors.tolist()
