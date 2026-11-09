#!/usr/bin/env python3
"""Serve the frozen un-fine-tuned ``m8_rrf`` retriever on loopback HTTP."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rag_retrieval.runtime import M8_RRF_PROFILE, M8RRFIndex, file_sha256

LOGGER = logging.getLogger("scat-rag-baseline")
MAX_REQUEST_BYTES = 64 * 1024


def load_dense_model(path: Path, device: str):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(str(path), device=device, local_files_only=True)


def build_handler(
    index: M8RRFIndex,
    *,
    generation_id: str,
    corpus_sha256: str,
    model_id: str,
) -> type[BaseHTTPRequestHandler]:
    model_lock = threading.Lock()
    started_at = time.time()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ScatRAGBaseline/1.0"

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/health", "/health/ready"}:
                self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ready",
                    "profile": M8_RRF_PROFILE,
                    "generation_id": generation_id,
                    "corpus_sha256": corpus_sha256,
                    "chunk_count": len(index.chunks),
                    "model_id": model_id,
                    "uptime_seconds": round(time.time() - started_at, 1),
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/search":
                self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 0 < content_length <= MAX_REQUEST_BYTES:
                    raise ValueError("invalid Content-Length")
                payload = json.loads(self.rfile.read(content_length))
                query = str(payload.get("query", "")).strip()
                limit = int(payload.get("limit", 5))
                if not query or len(query) > 1000:
                    raise ValueError("query must contain 1-1000 characters")
                if not 1 <= limit <= 10:
                    raise ValueError("limit must be between 1 and 10")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "invalid_request", "message": str(exc)},
                )
                return
            started = time.perf_counter()
            try:
                with model_lock:
                    results = index.search(query, limit=limit)
            except Exception:
                LOGGER.exception("Retrieval failed")
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"status": "internal_error", "message": "retrieval failed"},
                )
                return
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "profile_id": M8_RRF_PROFILE["profile_id"],
                    "fine_tuned": False,
                    "generation_id": generation_id,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "count": len(results),
                    "results": results,
                },
            )

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), format % args)

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--dense-model", type=Path, required=True)
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The baseline retrieval service may only bind to loopback")
    generation = json.loads(args.generation.read_text(encoding="utf-8"))
    model_id = str(args.dense_model.resolve())
    LOGGER.info("Loading un-fine-tuned dense model: %s", model_id)
    model = load_dense_model(args.dense_model, args.device)
    index = M8RRFIndex.from_paths(
        chunks_path=args.chunks,
        model=model,
        model_id=model_id,
        cache_path=args.embedding_cache,
        batch_size=args.batch_size,
    )
    handler = build_handler(
        index,
        generation_id=str(generation["generation_id"]),
        corpus_sha256=file_sha256(args.chunks),
        model_id=model_id,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    LOGGER.info(
        "Ready on http://%s:%s with %s chunks (%s)",
        args.host,
        args.port,
        len(index.chunks),
        M8_RRF_PROFILE["profile_id"],
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    main()
