"""Loopback-only, pinned BGE/RRF Top100 plus Qwen3-0.6B candidate service."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-rag-train/bin/python":
        raise ValueError("use_existing_scat_rag_train")
    stage = ROOT / ".tmp/rag-06b/service"
    stage.mkdir(parents=True, exist_ok=True)
    for name in (
        "TMPDIR",
        "TMP",
        "TEMP",
        "HF_HOME",
        "XDG_CACHE_HOME",
        "TORCH_HOME",
        "CUDA_CACHE_PATH",
        "PYTORCH_KERNEL_CACHE_PATH",
    ):
        os.environ[name] = str(stage)
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="4",
    )
    sys.path.insert(0, str(ROOT / "src"))
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    from hermes_runtime.real_rag import digest
    from rag_retrieval.rerank_comparison import Qwen3Reranker, frozen_candidates, order_by_scores
    from rag_retrieval.runtime import M8RRFIndex, build_jieba_tokenizer, load_chunk_records

    manifest = json.loads((ROOT / "services/rag/qwen3-06b-download.json").read_text())
    config = json.loads((ROOT / "services/rag/qwen3-06b-dev40-v1.json").read_text())
    for row in manifest["files"]:
        path = Path(manifest["destination"]) / row["path"]
        if path.stat().st_size != row["size"] or digest(path.read_bytes()) != row["sha256"]:
            raise ValueError("model_file_identity_mismatch")
    for name in ("chunks", "embedding_cache", "embedding_cache_metadata"):
        row = config["inputs"][name]
        if digest(Path(row["path"]).read_bytes()) != row["sha256"]:
            raise ValueError("retriever_file_identity_mismatch")
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    chunks, records = load_chunk_records(Path(config["inputs"]["chunks"]["path"]))
    model = SentenceTransformer(
        "/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181",
        device="cuda",
        local_files_only=True,
    )
    vectors = np.load(config["inputs"]["embedding_cache"]["path"], mmap_mode="r")
    index = M8RRFIndex(
        chunks=chunks,
        records=records,
        model=model,
        corpus_vectors=vectors,
        tokenizer=build_jieba_tokenizer(),
    )
    reranker = Qwen3Reranker(manifest["destination"], batch_size=4, max_length=2048)
    smoke, _ = reranker.score(
        "What is the capital of China?", ["Beijing is the capital of China.", "Water is a liquid."]
    )
    if smoke[0] <= smoke[1]:
        raise ValueError("reranker_smoke_failed")
    lock = threading.Lock()
    identity = {
        "status": "ready",
        "generation_id": "kg_20260926_research_v3",
        "corpus_sha256": config["inputs"]["chunks"]["sha256"],
        "chunk_count": len(chunks),
        "reranker_revision": manifest["huggingface_revision"],
        "profile_id": "m8_rrf_qwen06b_top100",
        "candidate_limit": 100,
    }

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send(
                200 if self.path == "/health" else 404,
                identity if self.path == "/health" else {"status": "not_found"},
            )

        def do_POST(self):
            if self.path != "/search":
                self.send(404, {"status": "not_found"})
                return
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 8192:
                    raise ValueError("invalid_request")
                request = json.loads(self.rfile.read(size))
                query = request["query"]
                if (
                    set(request) - {"query", "limit"}
                    or not isinstance(query, str)
                    or not 1 <= len(query.strip()) <= 1000
                    or request.get("limit", 5) != 5
                ):
                    raise ValueError("invalid_request")
            except (KeyError, ValueError, TypeError):
                self.send(HTTPStatus.BAD_REQUEST, {"status": "invalid_request"})
                return
            started = time.monotonic()
            try:
                with lock:
                    ids = frozen_candidates(index, query, 100)
                    scores, info = reranker.score(
                        query, [index.chunks_by_id[cid].retrieval_text() for cid in ids]
                    )
                    if info["truncated_pairs"]:
                        raise ValueError("candidate_context_would_truncate")
                    ranked = order_by_scores(ids, scores)
                    results = [
                        {
                            **{
                                k: records[cid][k]
                                for k in ("chunk_id", "source_id", "source_version", "text")
                            },
                            "rank": rank,
                            "section_path": records[cid].get("section_path", []),
                            "locator": records[cid].get("locator"),
                        }
                        for rank, cid in enumerate(ranked[:5], 1)
                    ]
                self.send(
                    200,
                    {
                        **identity,
                        "status": "ok",
                        "results": results,
                        "latency_ms": (time.monotonic() - started) * 1000,
                    },
                )
            except (ValueError, RuntimeError):
                self.send(503, {"status": "retrieval_unavailable"})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 18133), Handler)
    server.daemon_threads = True
    timer = threading.Timer(4 * 3600, server.shutdown)
    timer.daemon = True
    timer.start()
    print(json.dumps(identity), flush=True)
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
