"""Online runtime for the frozen, un-fine-tuned M8 RRF retrieval baseline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from rag_retrieval.baseline import BM25Index, Chunk, reciprocal_rank_fusion
from rag_retrieval.enhancements import (
    apply_source_boost,
    infer_source_ids,
    rank_exact_identifier_matches,
)

M8_RRF_PROFILE: dict[str, Any] = {
    "profile_id": "m8_rrf",
    "tokenizer": "jieba-search-v1",
    "candidate_k": 200,
    "bm25_k1": 0.8,
    "bm25_b": 0.3,
    "rrf_k": 40,
    "exact_weight": 1.0,
    "source_multiplier": 2.2,
    "dense_model": "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181",
    "fine_tuned": False,
}


class DenseEncoder(Protocol):
    def encode(self, sentences: Sequence[str], **kwargs: Any) -> Any: ...


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_jieba_tokenizer() -> Callable[[str], list[str]]:
    """Create the tokenizer used by the frozen M8 profile."""

    try:
        import jieba
    except ImportError as exc:  # pragma: no cover - production-only dependency
        raise RuntimeError("m8_rrf requires jieba==0.42.1") from exc

    jieba.setLogLevel(20)

    def tokenize(text: str) -> list[str]:
        tokens = [token.strip() for token in jieba.cut_for_search(text.casefold())]
        return [token for token in tokens if token and not token.isspace()]

    return tokenize


def load_chunk_records(path: Path) -> tuple[list[Chunk], dict[str, dict[str, Any]]]:
    chunks: list[Chunk] = []
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = json.loads(line)
        chunk_id = str(raw["chunk_id"])
        if chunk_id in records:
            raise ValueError(f"Duplicate chunk ID at line {line_number}: {chunk_id}")
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                source_id=str(raw["source_id"]),
                source_version=str(raw["source_version"]),
                section_path=tuple(raw.get("section_path", [])),
                text=str(raw["text"]),
                exact_terms=tuple(raw.get("exact_terms", [])),
            )
        )
        records[chunk_id] = raw
    if not chunks:
        raise ValueError("Knowledge corpus is empty")
    return chunks, records


def _load_or_create_vectors(
    *,
    model: DenseEncoder,
    texts: Sequence[str],
    cache_path: Path | None,
    corpus_sha256: str,
    model_id: str,
    batch_size: int,
) -> np.ndarray:
    expected = {
        "schema_version": "scat-rag-online-dense-cache/1.0",
        "corpus_sha256": corpus_sha256,
        "model_id": model_id,
        "normalized": True,
        "row_count": len(texts),
    }
    if cache_path is not None and cache_path.is_file():
        metadata_path = cache_path.with_suffix(cache_path.suffix + ".json")
        if metadata_path.is_file():
            actual = json.loads(metadata_path.read_text(encoding="utf-8"))
            vectors = np.load(cache_path)
            if actual == expected and vectors.ndim == 2 and vectors.shape[0] == len(texts):
                return np.asarray(vectors, dtype=np.float32)

    vectors = np.asarray(
        model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        ),
        dtype=np.float32,
    )
    if vectors.ndim != 2 or vectors.shape[0] != len(texts):
        raise ValueError("Dense model returned an invalid corpus embedding matrix")
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache_path, vectors)
        cache_path.with_suffix(cache_path.suffix + ".json").write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return vectors


class M8RRFIndex:
    """In-memory implementation of the frozen ``m8_rrf`` arm."""

    def __init__(
        self,
        *,
        chunks: Sequence[Chunk],
        records: dict[str, dict[str, Any]],
        model: DenseEncoder,
        corpus_vectors: np.ndarray,
        tokenizer: Callable[[str], list[str]],
    ) -> None:
        if corpus_vectors.ndim != 2 or corpus_vectors.shape[0] != len(chunks):
            raise ValueError("Corpus vector count does not match chunk count")
        self.chunks = tuple(chunks)
        self.records = records
        self.model = model
        self.corpus_vectors = np.asarray(corpus_vectors, dtype=np.float32)
        self.chunk_ids = tuple(chunk.chunk_id for chunk in chunks)
        self.chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self.bm25 = BM25Index(
            chunks,
            tokenizer=tokenizer,
            k1=M8_RRF_PROFILE["bm25_k1"],
            b=M8_RRF_PROFILE["bm25_b"],
        )

    @classmethod
    def from_paths(
        cls,
        *,
        chunks_path: Path,
        model: DenseEncoder,
        model_id: str,
        cache_path: Path | None = None,
        batch_size: int = 16,
        tokenizer: Callable[[str], list[str]] | None = None,
    ) -> M8RRFIndex:
        chunks, records = load_chunk_records(chunks_path)
        vectors = _load_or_create_vectors(
            model=model,
            texts=[chunk.retrieval_text() for chunk in chunks],
            cache_path=cache_path,
            corpus_sha256=file_sha256(chunks_path),
            model_id=model_id,
            batch_size=batch_size,
        )
        return cls(
            chunks=chunks,
            records=records,
            model=model,
            corpus_vectors=vectors,
            tokenizer=tokenizer or build_jieba_tokenizer(),
        )

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        normalized = " ".join(query.split())
        if not normalized:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")

        candidate_k = int(M8_RRF_PROFILE["candidate_k"])
        lexical_ids = [chunk_id for chunk_id, _ in self.bm25.rank(normalized, limit=candidate_k)]
        query_vector = np.asarray(
            self.model.encode([normalized], normalize_embeddings=True, show_progress_bar=False)[0],
            dtype=np.float32,
        )
        scores = self.corpus_vectors @ query_vector
        dense_order = np.argsort(-scores, kind="stable")[:candidate_k]
        dense_ids = [self.chunk_ids[index] for index in dense_order]
        exact_ids = [
            chunk_id
            for chunk_id, _ in rank_exact_identifier_matches(
                normalized, self.chunks, limit=candidate_k
            )
        ]

        rankings = [lexical_ids, dense_ids]
        weights = [1.0, 1.0]
        if exact_ids:
            rankings.append(exact_ids)
            weights.append(float(M8_RRF_PROFILE["exact_weight"]))
        fused = reciprocal_rank_fusion(
            rankings,
            rank_constant=int(M8_RRF_PROFILE["rrf_k"]),
            weights=weights,
            limit=candidate_k,
        )
        boosted = apply_source_boost(
            fused,
            self.chunks_by_id,
            infer_source_ids(normalized),
            float(M8_RRF_PROFILE["source_multiplier"]),
        )

        lexical_rank = {chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, start=1)}
        dense_rank = {chunk_id: rank for rank, chunk_id in enumerate(dense_ids, start=1)}
        exact_rank = {chunk_id: rank for rank, chunk_id in enumerate(exact_ids, start=1)}
        results: list[dict[str, Any]] = []
        for rank, (chunk_id, score) in enumerate(boosted[:limit], start=1):
            raw = self.records[chunk_id]
            results.append(
                {
                    "rank": rank,
                    "chunk_id": chunk_id,
                    "source_id": raw["source_id"],
                    "source_version": raw["source_version"],
                    "section_path": raw.get("section_path", []),
                    "locator": raw.get("locator"),
                    "text": raw["text"],
                    "score": round(float(score), 8),
                    "signals": {
                        "bm25_rank": lexical_rank.get(chunk_id),
                        "dense_rank": dense_rank.get(chunk_id),
                        "exact_rank": exact_rank.get(chunk_id),
                    },
                }
            )
        return results


__all__ = [
    "DenseEncoder",
    "M8RRFIndex",
    "M8_RRF_PROFILE",
    "build_jieba_tokenizer",
    "file_sha256",
    "load_chunk_records",
]
