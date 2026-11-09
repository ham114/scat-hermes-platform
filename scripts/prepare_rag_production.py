"""Create an immutable, corrected corpus and an incremental BGE cache in this project."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OLD = Path("/mnt/h3c/scat-rag/corpora/normalized/kg_20260926_research_v3")
TARGET = ROOT / "var/rag/corpora/kg_20261026_research_v4"
MODEL = "/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-rag-train/bin/python":
        raise ValueError("use_existing_scat_rag_train")
    os.umask(0o077)
    stage = ROOT / ".tmp/rag-live/prepare"
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP", "HF_HOME", "XDG_CACHE_HOME", "CUDA_CACHE_PATH"):
        os.environ[name] = str(stage)
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="4",
    )
    sys.path.insert(0, str(ROOT / "src"))
    import numpy as np
    from sentence_transformers import SentenceTransformer

    from rag_retrieval.baseline import tokenize_unicode
    from rag_retrieval.runtime import load_chunk_records

    if (TARGET / "ready.json").exists():
        raise ValueError("immutable_generation_already_exists")
    original = (OLD / "rag_chunks.v1.jsonl").read_bytes()
    if sha(original) != "61788775e03c392b493a7835bba2e5d38d7f8721035b73ccbb1dda441eb53b86":
        raise ValueError("parent_corpus_changed")
    rows = [json.loads(line) for line in original.decode().splitlines()]
    revised = copy.deepcopy(rows)
    changed = []
    for i, row in enumerate(revised):
        if row["source_id"] == "scat-dataset-curated" and "Table 1B：" in " > ".join(
            row["section_path"]
        ):
            counts = [
                int(n.replace(",", ""))
                for n in re.findall(
                    r"^\|\s*`scat\d+_\d+\.zip`[^\n]+\|\s*([\d,]+)\s*\|$", row["text"], re.MULTILINE
                )
            ]
            if len(counts) != 7 or sum(counts) != 94992:
                raise ValueError("table_arithmetic_not_verified")
            old = "| **合计** | - | - | **167,547** |"
            if row["text"].count(old) != 1:
                raise ValueError("unexpected_table_total")
            row["text"] = row["text"].replace(old, "| **本表7包小计** | - | - | **94,992** |")
            row["text"] += (
                "\n\n修订说明：Table 1A六包小计72,555，Table 1B七包小计94,992；Table 1A与1B全部十三包总计167,547。原分表合计行误标全量总计，现明确分表与全量范围。"
            )
            row["text_sha256"] = sha(row["text"].encode())
            row["char_count"] = len(row["text"])
            row["token_count"] = len(tokenize_unicode(row["text"]))
            changed.append(i)
    if len(changed) != 1:
        raise ValueError("expected_exactly_one_text_revision")
    source_text = "\n\n".join(
        row["text"] for row in revised if row["source_id"] == "scat-dataset-curated"
    )
    source_sha = sha(source_text.encode())
    mapping = {}
    for old, new in zip(rows, revised, strict=True):
        if new["source_id"] == "scat-dataset-curated":
            new["source_version"] = "chunks-sha256-" + source_sha[:12]
            new["source_content_sha256"] = source_sha
            identity = {k: new[k] for k in ("source_id", "source_version", "locator", "text")}
            new["chunk_id"] = (
                "kc2_" + sha(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode())[:32]
            )
        mapping[old["chunk_id"]] = new["chunk_id"]
    TARGET.mkdir(parents=True, exist_ok=True)
    (TARGET / "scat-dataset-curated.normalized-source.txt").write_text(
        source_text, encoding="utf-8"
    )
    path = TARGET / "rag_chunks.v1.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in revised),
        encoding="utf-8",
    )
    corpus_sha = sha(path.read_bytes())
    parent_vectors = Path(
        "/home/h3c/project/python/agent-service-toolkit-main/var/rag-baseline/research-v3-base-bge-m3.npy"
    )
    if (
        sha(parent_vectors.read_bytes())
        != "897848e305f24984b5255cd1ca8a73715f7232366bd5ef29b28849df5d1ca9c3"
    ):
        raise ValueError("parent_vectors_changed")
    vectors = np.load(parent_vectors).copy()
    chunks, _ = load_chunk_records(path)
    model = SentenceTransformer(MODEL, device="cuda", local_files_only=True)
    vectors[changed] = model.encode(
        [chunks[i].retrieval_text() for i in changed],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    if (
        vectors.shape != (149, 1024)
        or not np.isfinite(vectors).all()
        or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4)
    ):
        raise ValueError("revised_embedding_cache_invalid")
    cache = TARGET / "bge-m3.npy"
    np.save(cache, vectors)
    generation = json.loads((OLD / "rag_generation.v1.json").read_text())
    generation.update(
        generation_id=TARGET.name,
        created_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        parser_id="scat-rag-normalized-revision",
        parser_version="1.0.0",
        parent_generation_id="kg_20260926_research_v3",
        parent_corpus_sha256=sha(original),
    )
    generation["notes"] = [
        "One deterministic subtotal scope correction; no original source or frozen dataset overwritten.",
        "Revised source digest is over the retained normalized source chunks, explicitly not the original Markdown bytes.",
        "kc2 ids bind source, derived version, locator and text; unchanged sources retain original ids.",
    ]
    for source in generation["sources"]:
        if source["source_id"] == "scat-dataset-curated":
            source.update(version="chunks-sha256-" + source_sha[:12], content_sha256=source_sha)
    generation.pop("corpus_manifest_sha256", None)
    save(TARGET / "rag_generation.v1.json", generation)
    save(
        TARGET / "revision.json",
        {
            "parent_corpus_sha256": sha(original),
            "corpus_sha256": corpus_sha,
            "changed_text_indices": changed,
            "rekeyed_chunks": sum(k != v for k, v in mapping.items()),
            "chunk_id_mapping": mapping,
            "embedding_rows_recomputed": changed,
            "old_frozen_datasets_modified": False,
        },
    )
    production = {
        "schema_version": 1,
        "profile_id": "m8_rrf_qwen06b_top100",
        "generation_id": TARGET.name,
        "corpus": str(path),
        "corpus_sha256": corpus_sha,
        "chunk_count": 149,
        "embedding_model": MODEL,
        "embedding_cache": str(cache),
        "embedding_cache_sha256": sha(cache.read_bytes()),
        "model_manifest": "services/rag/qwen3-06b-download.json",
        "model_manifest_sha256": sha((ROOT / "services/rag/qwen3-06b-download.json").read_bytes()),
        "candidate_limit": 100,
        "batch_size": 4,
        "max_input_tokens": 2048,
        "host": "127.0.0.1",
        "port": 18133,
        "max_inflight": 2,
    }
    save(ROOT / "services/rag/qwen3-production-v1.json", production)
    native = json.loads((ROOT / "services/rag/real-agent-06b-service.json").read_text())
    native.update(
        corpus=str(path),
        corpus_sha256=corpus_sha,
        generation_id=TARGET.name,
        scope="production-knowledge-rag-v1",
    )
    save(ROOT / "services/rag/real-agent-production-v1.json", native)
    save(
        TARGET / "ready.json",
        {
            "corpus_sha256": corpus_sha,
            "cache_sha256": production["embedding_cache_sha256"],
            "chunks": 149,
            "sources": 8,
            "changed_text_chunks": 1,
            "source_rekeyed_chunks": 27,
        },
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "directory": str(TARGET),
                "corpus_sha256": corpus_sha,
                "changed_text_chunks": 1,
                "rekeyed_chunks": 27,
            }
        )
    )


if __name__ == "__main__":
    main()
