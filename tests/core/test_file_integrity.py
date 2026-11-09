from __future__ import annotations

from pathlib import Path

import pytest

from core.file_integrity import FileIntegrityError, file_sha256, verify_sha256


def test_verify_sha256_accepts_registered_file_and_rejects_mutation(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"reviewed-model")
    expected = file_sha256(artifact)

    assert verify_sha256(artifact, expected, label="model") == expected
    artifact.write_bytes(b"mutated-model")
    with pytest.raises(FileIntegrityError, match="integrity check failed"):
        verify_sha256(artifact, expected, label="model")


def test_verify_sha256_rejects_noncanonical_digest(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"data")
    with pytest.raises(ValueError, match="64 lowercase"):
        verify_sha256(artifact, "ABC", label="model")
