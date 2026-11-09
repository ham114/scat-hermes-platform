"""Authenticated CPU-only BGE-M3 OpenAI-compatible embeddings using existing Conda."""

import base64
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = Path("/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181")


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-rag-train/bin/python":
        raise ValueError("existing_RAG_Conda_required")
    os.umask(0o077)
    from hermes_runtime.skills import checked_path

    temporary = checked_path(
        ROOT, os.environ.get("SCAT_EMBEDDING_TEMP", ".tmp/honcho-test/embeddings")
    )
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        CUDA_VISIBLE_DEVICES="",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        TMPDIR=str(temporary),
        TEMP=str(temporary),
        TMP=str(temporary),
        XDG_CACHE_HOME=str(temporary),
    )
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    model = SentenceTransformer(str(MODEL), device="cpu", local_files_only=True)
    model.max_seq_length = 8192
    access = (
        checked_path(
            ROOT,
            os.environ.get("SCAT_EMBEDDING_SECRET_FILE", "var/deploy/honcho/secrets/embedding.key"),
        )
        .read_text()
        .strip()
    )
    gate = threading.BoundedSemaphore(1)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(60)

        def send(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.send(
                200 if self.path == "/health" else 404,
                {"status": "ready", "model": "bge-m3", "dimensions": 1024, "device": "cpu"},
            )

        def do_POST(self):
            if not secrets.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + access
            ):
                self.send(401, {"error": "unauthorized"})
                return
            if self.path != "/v1/embeddings":
                self.send(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 262144:
                    raise ValueError
                value = json.loads(self.rfile.read(length))
                texts = value.get("input")
                texts = [texts] if isinstance(texts, str) else texts
                if (
                    value.get("model") != "bge-m3"
                    or not isinstance(texts, list)
                    or not 1 <= len(texts) <= 32
                    or not all(isinstance(text, str) and 0 < len(text) <= 25000 for text in texts)
                ):
                    raise ValueError
                if (
                    value.get("encoding_format", "float") not in {"float", "base64"}
                    or value.get("dimensions", 1024) != 1024
                ):
                    raise ValueError
                ids = model.tokenizer(texts, truncation=False, add_special_tokens=True)["input_ids"]
                if any(len(row) > 8192 for row in ids):
                    self.send(422, {"error": "embedding_input_would_truncate"})
                    return
            except (ValueError, TypeError, AttributeError):
                self.send(400, {"error": "invalid_request"})
                return
            if not gate.acquire(blocking=False):
                self.send(429, {"error": "embedding_busy"})
                return
            try:
                vectors = model.encode(
                    texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False
                )
                if vectors.shape != (len(texts), 1024) or not np.isfinite(vectors).all():
                    raise RuntimeError("invalid_embedding_output")
                count = sum(len(row) for row in ids)
                self.send(
                    200,
                    {
                        "object": "list",
                        "model": "bge-m3",
                        "data": [
                            {
                                "object": "embedding",
                                "index": i,
                                "embedding": base64.b64encode(
                                    np.asarray(row, dtype="<f4").tobytes()
                                ).decode()
                                if value.get("encoding_format") == "base64"
                                else row.tolist(),
                            }
                            for i, row in enumerate(vectors)
                        ],
                        "usage": {"prompt_tokens": count, "total_tokens": count},
                    },
                )
            except Exception as exc:  # noqa: BLE001 -- return only failure type over HTTP
                self.send(503, {"error": type(exc).__name__})
            finally:
                gate.release()

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", 18141), Handler).serve_forever()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    main()
