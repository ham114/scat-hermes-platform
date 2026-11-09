"""Streaming file-integrity checks for production artifacts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class FileIntegrityError(RuntimeError):
    """Raised when an artifact does not match its registered digest."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str, *, label: str) -> str:
    if not SHA256_PATTERN.fullmatch(expected):
        raise ValueError(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")
    actual = file_sha256(path)
    if actual != expected:
        raise FileIntegrityError(f"{label} integrity check failed")
    return actual


__all__ = ["FileIntegrityError", "SHA256_PATTERN", "file_sha256", "verify_sha256"]
