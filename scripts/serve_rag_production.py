"""Run the pinned 0.6B retriever under the existing Anaconda environment."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from http.server import ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-rag-train/bin/python":
        raise ValueError("use_existing_scat_rag_train")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="services/rag/qwen3-production-v1.json")
    parser.add_argument("--port", type=int, choices=[18133, 18134])
    args = parser.parse_args()
    os.umask(0o077)
    runtime = ROOT / "var/rag/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
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
        os.environ[name] = str(runtime)
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="4",
    )
    sys.path.insert(0, str(ROOT / "src"))
    from rag_retrieval.production import Retriever, build_handler, load_config

    config = load_config(ROOT, Path(args.config))
    handler = RotatingFileHandler(
        runtime / "service.log", maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    engine = Retriever(ROOT, config)
    server = ThreadingHTTPServer(("127.0.0.1", args.port or config["port"]), build_handler(engine))
    server.daemon_threads = False
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(
            signum, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start()
        )
    LOGGER.info(
        "ready profile=%s generation=%s port=%s",
        config["profile_id"],
        config["generation_id"],
        server.server_port,
    )
    closed = threading.Event()

    def monitor():
        while not closed.wait(1):
            if engine.failed:
                server.shutdown()
                return

    threading.Thread(target=monitor, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        closed.set()
        server.server_close()
    if engine.failed:
        raise RuntimeError("retrieval_process_failed_restart_required")


if __name__ == "__main__":
    main()
