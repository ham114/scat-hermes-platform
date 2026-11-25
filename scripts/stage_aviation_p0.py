"""Embed a validated v6 candidate, reusing every unchanged v5 vector."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PARENT = ROOT / "var/rag/corpora/kg_20261123_aviation_v5"
TARGET = ROOT / "var/rag/corpora/kg_20261124_aviation_v6_p0"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")


def main():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="4")
    if (TARGET / "ready.json").exists():
        raise ValueError("v6_already_staged")
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    from rag_retrieval.runtime import load_chunk_records
    build = read(TARGET / "build-summary.json")
    assert build["status"] == "passed" and build["corpus_sha256"] == sha(TARGET / "rag_chunks.v1.jsonl")
    config = read(ROOT / "services/rag/qwen3-production-v1.json")
    assert config["generation_id"] == PARENT.name and config["corpus_sha256"] == sha(PARENT / "rag_chunks.v1.jsonl")
    assert config["embedding_cache_sha256"] == sha(PARENT / "bge-m3.npy")
    old, _ = load_chunk_records(PARENT / "rag_chunks.v1.jsonl")
    new, _ = load_chunk_records(TARGET / "rag_chunks.v1.jsonl")
    vectors = np.empty((len(new), 1024), dtype="float32")
    parent_vectors = np.load(PARENT / "bge-m3.npy", mmap_mode="r")
    by_id = {c.chunk_id: i for i, c in enumerate(old)}
    encode = []
    for i, c in enumerate(new):
        if c.chunk_id in by_id:
            vectors[i] = parent_vectors[by_id[c.chunk_id]]
        else:
            encode.append(i)
    baseline = {str(p.relative_to(ROOT)): sha(p) for p in [ROOT / "services/rag/qwen3-production-v1.json", ROOT / "services/rag/real-agent-production-v1.json",
                PARENT / "rag_chunks.v1.jsonl", PARENT / "bge-m3.npy"]}
    save(TARGET / "staging-inputs.json", baseline)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(config["embedding_model"], device=device, local_files_only=True)
    started = time.monotonic()
    for offset in range(0, len(encode), 64):
        indices = encode[offset:offset + 64]
        values = model.encode([new[i].retrieval_text() for i in indices], batch_size=4,
                              normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        vectors[indices] = values
        print(json.dumps({"embedded": offset + len(indices), "total": len(encode), "device": device}), flush=True)
    assert np.isfinite(vectors).all() and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-3)
    assert np.array_equal(vectors[:149], parent_vectors[:149])
    for i, c in enumerate(new):
        if c.chunk_id in by_id:
            assert np.array_equal(vectors[i], parent_vectors[by_id[c.chunk_id]])
    cache = TARGET / "bge-m3.npy"
    np.save(cache, vectors)
    candidate = {**config, "generation_id": TARGET.name, "corpus": str(TARGET / "rag_chunks.v1.jsonl"),
                 "corpus_sha256": build["corpus_sha256"], "chunk_count": len(new), "embedding_cache": str(cache), "embedding_cache_sha256": sha(cache)}
    native = read(ROOT / "services/rag/real-agent-production-v1.json")
    native.update(scope="production-knowledge-aviation-v6-p0", generation_id=TARGET.name,
                  corpus=candidate["corpus"], corpus_sha256=candidate["corpus_sha256"], corpus_count=len(new))
    save(TARGET / "retrieval-config.json", candidate)
    save(TARGET / "real-agent-config.json", native)
    generation = read(TARGET / "rag_generation.v1.json")
    generation.update(created_at=datetime.now(timezone.utc).isoformat(), status="ready-for-retrieval-validation",
                      parent_generation_metadata_sha256=sha(PARENT / "rag_generation.v1.json"),
                      quality={"scope": "P0 structural repair; not a full semantic validation of every document",
                               "tables_checked": build["table_count"], "nonempty_cells_checked": build["nonempty_cells_checked"],
                               "quarantined_tables": build["quarantined_tables"], "original_149_records_unchanged": True})
    save(TARGET / "rag_generation.v1.json", generation)
    save(TARGET / "ready.json", {"status": "ready-for-retrieval-validation", "generation_id": TARGET.name, "corpus_sha256": candidate["corpus_sha256"],
                                "chunk_count": len(new), "embedding_cache_sha256": sha(cache), "reused_vectors": len(new) - len(encode),
                                "new_vectors": len(encode), "embedding_seconds": time.monotonic() - started, "external_model_calls": 0,
                                "fine_tuned": False, "production_switched": False, "quarantined_tables": build["quarantined_tables"]})
    assert {p: sha(ROOT / p) for p in baseline} == baseline
    print(json.dumps(read(TARGET / "ready.json")), flush=True)


if __name__ == "__main__":
    main()
