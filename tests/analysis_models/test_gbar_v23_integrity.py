from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.file_integrity import FileIntegrityError, file_sha256
from scat_model_runtime.gbar_v23_adapter import GbarV23WorkerConfig


def _file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_worker_config_verifies_registered_model_files(tmp_path: Path) -> None:
    worker = _file(tmp_path / "worker.py", b"worker")
    config = _file(tmp_path / "config.json", b"{}")
    checkpoint = _file(tmp_path / "checkpoint.pt", b"weights")
    settings = GbarV23WorkerConfig(
        python_executable=Path(sys.executable),
        worker_script=worker,
        source_dir=tmp_path,
        model_config=config,
        checkpoint=checkpoint,
        source_uri="project-internal://scat-gbar-v23",
        worker_script_sha256=file_sha256(worker),
        model_config_sha256=file_sha256(config),
        checkpoint_sha256=file_sha256(checkpoint),
    )

    validated = settings.validated()
    assert validated.checkpoint_sha256 == file_sha256(checkpoint)

    checkpoint.write_bytes(b"changed")
    with pytest.raises(FileIntegrityError, match="checkpoint integrity"):
        settings.validated()
