"""Download pinned public Qwen weights; stage only inside the project.

Run with the existing server scat-rag-train Anaconda interpreter. This does not
install packages, run models, change an index, or start a service.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / ".tmp/qwen3-download"
REPORT = ROOT / "var/validation/20261025-Qwen3-4B-download.json"
INTERPRETER = "/home/h3c/anaconda3/envs/scat-rag-train/bin/python"
LOCK = ROOT / "var/validation/20261025-Qwen3-4B-download.lock"
GUARD = threading.Lock()
STATE: dict = {"status": "running", "models": {}, "started_at": None}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def record(kind: str, **values) -> None:
    with GUARD:
        STATE["models"].setdefault(kind, {}).update(values)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        # A diagnostic progress report is a retained project artifact.
        REPORT.write_text(json.dumps(STATE, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"model": kind, **values}), flush=True)


def safe_path(base: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative:
        raise ValueError("invalid_manifest_path")
    target = base / relative
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError("path_escape")
    for parent in (target, *target.parents):
        if parent == base.parent:
            break
        if parent.is_symlink():
            raise ValueError("symlink_not_allowed")
    return target


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host == "modelscope.cn" or host.endswith(".modelscope.cn")
        ):
            raise ValueError("unexpected_download_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def install_dns_fallback(path: Path | None) -> None:
    if path is None:
        return
    if not path.resolve().is_relative_to(STAGE.resolve()):
        raise ValueError("dns_input_outside_project_stage")
    config = json.loads(path.read_text())
    created = config["created_at"]
    if not 0 <= time.time() - created < 7200:
        raise ValueError("expired_dns_input")
    addresses = config["addresses"]
    if set(addresses) - {"modelscope.cn", "cdn-lfs-cn-1.modelscope.cn"}:
        raise ValueError("unexpected_dns_host")
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host in addresses and time.time() - created < 7200:
            results = []
            for address in addresses[host]:
                socket.inet_pton(socket.AF_INET, address)
                results.extend(original(address, port, *args, **kwargs))
            return results
        return original(host, port, *args, **kwargs)

    socket.getaddrinfo = resolve


def fetch(model: dict, row: dict, destination: Path) -> None:
    target = safe_path(destination, row["path"])
    if target.exists():
        if target.stat().st_size != row["size"] or digest(target) != row["sha256"]:
            raise ValueError("existing_destination_differs")
        return
    partial = safe_path(STAGE / model["kind"], row["path"])
    partial.parent.mkdir(parents=True, exist_ok=True)
    url = (
        f"https://modelscope.cn/models/{model['repo']}/resolve/"
        f"{model['modelscope_revision']}/{urllib.parse.quote(row['path'], safe='/')}"
    )
    opener = urllib.request.build_opener(OfficialRedirect)
    deadline = time.monotonic() + 2400
    for attempt in range(5):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > row["size"]:
                raise ValueError("oversized_stage")
            if offset < row["size"]:
                headers = {"User-Agent": "scat-hermes-pinned-model-download/1"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(url, headers=headers)
                with opener.open(request, timeout=30) as response:
                    if offset and (
                        response.status != 206
                        or not response.headers.get("Content-Range", "").startswith(
                            f"bytes {offset}-"
                        )
                    ):
                        raise ValueError("resume_range_not_honored")
                    last = time.monotonic()
                    with partial.open("ab" if offset else "wb") as stream:
                        while block := response.read(8 * 1024 * 1024):
                            offset += len(block)
                            if offset > row["size"] or time.monotonic() > deadline:
                                raise ValueError("download_size_or_time_limit")
                            stream.write(block)
                            if time.monotonic() - last > 10:
                                record(
                                    model["kind"],
                                    file=row["path"],
                                    bytes=offset,
                                    file_bytes=row["size"],
                                    attempt=attempt + 1,
                                )
                                last = time.monotonic()
            if partial.stat().st_size != row["size"]:
                raise ValueError("incomplete_download")
            if digest(partial) != row["sha256"]:
                partial.unlink()
                raise ValueError("download_sha256_mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            # /mnt and the project are different filesystems. Only verified
            # bytes are copied; no cache or temporary file is made in /mnt.
            created = False
            try:
                with partial.open("rb") as source, target.open("xb") as output:
                    created = True
                    shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if digest(target) != row["sha256"]:
                    raise ValueError("destination_sha256_mismatch")
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                raise
            return
        except (OSError, ValueError) as exc:
            record(
                model["kind"],
                file=row["path"],
                attempt=attempt + 1,
                error=type(exc).__name__,
                reason=str(exc) if isinstance(exc, ValueError) else "network_or_io",
            )
            if attempt == 4 or time.monotonic() > deadline:
                raise RuntimeError("download_failed") from None
            time.sleep(2)


def download(model: dict, verify_only: bool) -> None:
    kind = model["kind"]
    if (kind, model["repo"]) not in {
        ("reranker", "Qwen/Qwen3-Reranker-4B"),
        ("embedding", "Qwen/Qwen3-Embedding-4B"),
    } or not re.fullmatch(r"[0-9a-f]{40}", model["modelscope_revision"]):
        raise ValueError("unexpected_model")
    base = Path("/mnt/h3c/scat-rag/models") / kind
    destination = safe_path(base, Path(model["destination"]).name)
    if str(destination) != model["destination"]:
        raise ValueError("unexpected_destination")
    destination.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(model["files"]):
        if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ValueError("invalid_expected_digest")
        if not verify_only:
            fetch(model, row, destination)
        target = safe_path(destination, row["path"])
        if target.stat().st_size != row["size"] or digest(target) != row["sha256"]:
            raise ValueError("verification_failed")
        if not verify_only:
            safe_path(STAGE / kind, row["path"]).unlink(missing_ok=True)
        record(kind, verified_files=index + 1, file=row["path"], error=None)
    record(
        kind,
        status="verified",
        repo=model["repo"],
        destination=str(destination),
        revision=model["modelscope_revision"],
        total_bytes=model["total_bytes"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dns-fallback", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if sys.executable != INTERPRETER:
        raise ValueError("use_existing_scat_rag_train")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("x") as stream:
        stream.write(str(os.getpid()))
    try:
        STATE["started_at"] = time.time()
        install_dns_fallback(args.dns_fallback)
        manifest = ROOT / "services/rag/qwen3-4b-download.json"
        models = json.loads(manifest.read_text())["models"]
        if not args.verify_only and (
            shutil.disk_usage(ROOT).free < 16 * 1024**3
            or shutil.disk_usage("/mnt/h3c/scat-rag").free < 32 * 1024**3
        ):
            raise ValueError("insufficient_disk_headroom")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(download, model, args.verify_only) for model in models]
            for future in futures:
                future.result()
        STATE["status"] = "verified"
        STATE["finished_at"] = time.time()
        record("summary", model_quality_admitted=False, new_services=0)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        STATE["status"] = "failed"
        record(
            "summary",
            error=type(exc).__name__,
            reason=str(exc) if isinstance(exc, ValueError) else "download_or_io_failure",
        )
        return 1
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
