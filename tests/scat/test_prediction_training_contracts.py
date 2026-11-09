"""M6-02 训练侧 schema 测试：TrainingManifest / TrainingDatasetSpec 校验。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from scat.prediction.training import (
    TRAINING_SCHEMA_VERSION,
    TrainingArtifact,
    TrainingManifest,
)

T0 = datetime(2016, 10, 20, 12, 0, 0, tzinfo=UTC)


def _payload() -> dict[str, Any]:
    return {
        "predictor_id": "kalman-cv",
        "predictor_version": "2",
        "dataset": {
            "batch": "scat20161015_20161021",
            "source_version": "scat-import-2",
            "feature_version": "features-v3",
            "split_method": "by_flight",
            "seed": 42,
            "ratios": {"train": 0.7, "validation": 0.15, "test": 0.15},
            "train_flight_count": 700,
            "validation_flight_count": 150,
            "test_flight_count": 150,
            "flight_ids_sha256": {"train": "a" * 64, "test": "b" * 64},
        },
        "hyperparameters": {"window_s": 300.0},
        "model_artifacts": [
            {"name": "model.pkl", "path": "run/model.pkl", "sha256": "c" * 64, "size": 10}
        ],
        "train_log_artifact": {
            "name": "train.log",
            "path": "run/train.log",
            "sha256": "d" * 64,
            "size": 20,
        },
        "evaluation_experiment_id": "m6eval-kalman-cv-scat20161015_20161021-20260920T000000-abc123",
        "code_version": "0.1.0",
        "started_at": T0,
    }


def test_manifest_accepts_complete_training_run() -> None:
    manifest = TrainingManifest.model_validate(_payload())

    assert manifest.schema_version == TRAINING_SCHEMA_VERSION
    assert manifest.dataset.split_method == "by_flight"
    assert manifest.dataset.seed == 42
    assert manifest.model_artifacts[0].size == 10
    assert manifest.train_log_artifact is not None
    assert manifest.finished_at is None


def test_manifest_rejects_bad_digest_and_unknown_split() -> None:
    with pytest.raises(ValidationError):
        TrainingManifest.model_validate(
            _payload()
            | {"model_artifacts": [{"name": "m", "path": "p", "sha256": "XYZ", "size": 1}]}
        )

    dataset = {**_payload()["dataset"], "ratios": {"train": 0.5, "holdout": 0.5}}
    with pytest.raises(ValidationError):
        TrainingManifest.model_validate(_payload() | {"dataset": dataset})


def test_manifest_rejects_empty_split() -> None:
    dataset = {
        **_payload()["dataset"],
        "train_flight_count": 0,
        "validation_flight_count": 0,
        "test_flight_count": 0,
    }

    with pytest.raises(ValidationError):
        TrainingManifest.model_validate(_payload() | {"dataset": dataset})


def test_training_artifact_requires_lowercase_hex_digest() -> None:
    with pytest.raises(ValidationError):
        TrainingArtifact(name="m", path="p", sha256="A" * 64, size=1)
