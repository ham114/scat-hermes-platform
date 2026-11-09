"""Pinned public 0.6B weights, existing Conda, project-only staging."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import download_qwen3_rag_candidates as shared

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-rag-train/bin/python":
        raise ValueError("use_existing_scat_rag_train")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dns-fallback", type=Path)
    args = parser.parse_args()
    shared.STAGE = ROOT / ".tmp/rag-06b/download"
    shared.REPORT = ROOT / "var/validation/20261026-Qwen3-06B-download.json"
    shared.STAGE.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME"):
        os.environ[name] = str(shared.STAGE)
    shared.install_dns_fallback(args.dns_fallback)
    model = json.loads((ROOT / "services/rag/qwen3-06b-download.json").read_text())
    if model["repo"] != "Qwen/Qwen3-Reranker-0.6B":
        raise ValueError("unexpected_06b_model")
    destination = Path(model["destination"])
    if destination.parent != Path("/mnt/h3c/scat-rag/models/reranker") or destination.is_symlink():
        raise ValueError("unexpected_model_destination")
    destination.mkdir(exist_ok=True)
    lock = ROOT / "var/validation/20261026-Qwen3-06B-download.lock"
    with lock.open("x") as stream:
        stream.write(str(os.getpid()))
    try:
        for number, row in enumerate(model["files"], 1):
            shared.fetch(model, row, destination)
            target = shared.safe_path(destination, row["path"])
            if shared.digest(target) != row["sha256"] or target.stat().st_size != row["size"]:
                raise ValueError("download_identity_mismatch")
            shared.safe_path(shared.STAGE / model["kind"], row["path"]).unlink(missing_ok=True)
            shared.record("reranker", verified_files=number, status="downloading")
        shared.record(
            "reranker",
            status="verified",
            model=model["repo"],
            revision=model["huggingface_revision"],
            total_bytes=model["total_bytes"],
        )
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
