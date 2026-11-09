"""Pinned production retrieval and a bounded, legacy-compatible loopback HTTP contract."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_config(root, path):
    root = Path(root).resolve()
    path = (root / path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("configuration_outside_project")
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version": 1,
        "profile_id": "m8_rrf_qwen06b_top100",
        "candidate_limit": 100,
        "batch_size": 4,
        "max_input_tokens": 2048,
        "host": "127.0.0.1",
    }
    if any(config.get(k) != v for k, v in required.items()):
        raise ValueError("unapproved_production_retriever")
    if config["port"] not in {18133, 18134} or config["max_inflight"] != 2:
        raise ValueError("unapproved_production_listener")
    for key in ("corpus", "embedding_cache"):
        if digest(config[key]) != config[key + "_sha256"]:
            raise ValueError("artifact_identity_mismatch:" + key)
    manifest_path = (root / config["model_manifest"]).resolve()
    if (
        not manifest_path.is_relative_to(root)
        or digest(manifest_path) != config["model_manifest_sha256"]
    ):
        raise ValueError("model_manifest_changed")
    return config


class Retriever:
    def __init__(self, root, config):
        import numpy as np
        import torch
        from sentence_transformers import SentenceTransformer

        from rag_retrieval.rerank_comparison import Qwen3Reranker
        from rag_retrieval.runtime import M8RRFIndex, build_jieba_tokenizer, load_chunk_records

        manifest = json.loads((Path(root) / config["model_manifest"]).read_text())
        if manifest["huggingface_revision"] != "e61197ed45024b0ed8a2d74b80b4d909f1255473":
            raise ValueError("reranker_revision_changed")
        for row in manifest["files"]:
            path = Path(manifest["destination"]) / row["path"]
            if path.stat().st_size != row["size"] or digest(path) != row["sha256"]:
                raise ValueError("reranker_file_changed")
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        if not torch.cuda.is_available():
            raise ValueError("production_cuda_required")
        chunks, self.records = load_chunk_records(Path(config["corpus"]))
        if len(chunks) != config["chunk_count"]:
            raise ValueError("corpus_count_changed")
        if any(
            hashlib.sha256(row["text"].encode()).hexdigest() != row["text_sha256"]
            for row in self.records.values()
        ):
            raise ValueError("corpus_text_digest_changed")
        vectors = np.load(config["embedding_cache"], mmap_mode="r")
        if vectors.shape != (len(chunks), 1024) or not np.isfinite(vectors).all():
            raise ValueError("invalid_vector_cache")
        model = SentenceTransformer(config["embedding_model"], device="cuda", local_files_only=True)
        self.index = M8RRFIndex(
            chunks=chunks,
            records=self.records,
            model=model,
            corpus_vectors=vectors,
            tokenizer=build_jieba_tokenizer(),
        )
        self.reranker = Qwen3Reranker(manifest["destination"], batch_size=4, max_length=2048)
        scores, _ = self.reranker.score(
            "What is the capital of China?",
            ["Beijing is the capital of China.", "Water is a liquid."],
        )
        if scores[0] <= scores[1]:
            raise ValueError("production_reranker_smoke_failed")
        self.lock = threading.Lock()
        self.failed = False
        self.identity = {
            "status": "ready",
            "profile_id": config["profile_id"],
            "generation_id": config["generation_id"],
            "corpus_sha256": config["corpus_sha256"],
            "chunk_count": len(chunks),
            "candidate_limit": 100,
            "reranker_revision": manifest["huggingface_revision"],
            "fine_tuned": False,
        }

    def search(self, query, limit):
        from rag_retrieval.rerank_comparison import frozen_candidates, order_by_scores

        with self.lock:
            ids = frozen_candidates(self.index, query, 100)
            scores, info = self.reranker.score(
                query, [self.index.chunks_by_id[cid].retrieval_text() for cid in ids]
            )
            if info["truncated_pairs"] or not all(map(math.isfinite, scores)):
                raise ValueError("unscorable_query_without_truncation")
            ranked = order_by_scores(ids, scores)
            by_id = dict(zip(ids, scores, strict=True))
            return [
                {
                    "rank": rank,
                    **{
                        k: self.records[cid][k]
                        for k in ("chunk_id", "source_id", "source_version", "text", "text_sha256")
                    },
                    "section_path": self.records[cid].get("section_path", []),
                    "locator": self.records[cid].get("locator"),
                    "score": float(by_id[cid]),
                    "signals": {"reranker": "qwen3-0.6b", "candidate_rank": ids.index(cid) + 1},
                }
                for rank, cid in enumerate(ranked[:limit], 1)
            ]


def build_handler(engine):
    admission = threading.BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def send(self, status, data):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if status == 429:
                self.send_header("Retry-After", "2")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in {"/health", "/health/ready"}:
                self.send(404, {"status": "not_found"})
            elif engine.failed:
                self.send(503, {"status": "degraded"})
            else:
                self.send(200, engine.identity)

        def do_POST(self):
            if self.path != "/search":
                self.send(404, {"status": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid_body")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or set(request) - {"query", "limit"}:
                    raise ValueError("invalid_fields")
                query, limit = request.get("query"), request.get("limit", 5)
                if (
                    not isinstance(query, str)
                    or not 1 <= len(query.strip()) <= 1000
                    or type(limit) is not int
                    or not 1 <= limit <= 10
                ):
                    raise ValueError("invalid_query_or_limit")
            except (ValueError, TypeError):
                self.send(400, {"status": "invalid_request"})
                return
            if engine.failed:
                self.send(503, {"status": "retrieval_unavailable"})
                return
            if not admission.acquire(blocking=False):
                self.send(429, {"status": "busy"})
                return
            started = time.monotonic()
            try:
                results = engine.search(query.strip(), limit)
                self.send(
                    200,
                    engine.identity
                    | {
                        "status": "ok",
                        "count": len(results),
                        "results": results,
                        "latency_ms": round((time.monotonic() - started) * 1000, 3),
                    },
                )
                LOGGER.info(
                    "search status=ok count=%s elapsed_ms=%.3f",
                    len(results),
                    (time.monotonic() - started) * 1000,
                )
            except ValueError:
                self.send(422, {"status": "query_unscorable"})
            except RuntimeError as exc:
                engine.failed = True
                LOGGER.error("retrieval_runtime_error type=%s", type(exc).__name__)
                self.send(503, {"status": "retrieval_unavailable"})
            finally:
                admission.release()

        def log_message(self, *args):
            pass

    return Handler
