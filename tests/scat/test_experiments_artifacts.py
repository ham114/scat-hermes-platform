"""M6-07 产物存储测试：写入、校验和、路径安全与上限。"""

from __future__ import annotations

import pytest

from scat.experiments.artifacts import MAX_ARTIFACT_BYTES, ExperimentArtifactStore


def test_store_writes_artifact_with_digest_and_metadata(tmp_path) -> None:
    store = ExperimentArtifactStore(tmp_path)

    entry = store.store(
        experiment_id="m6eval-x-1",
        name="evaluation_report.json",
        payload={"a": 1},
        kind="report",
    )

    assert entry["path"] == "m6eval-x-1/evaluation_report.json"
    assert entry["kind"] == "report"
    assert len(entry["sha256"]) == 64
    data = (tmp_path / "m6eval-x-1" / "evaluation_report.json").read_bytes()
    assert entry["size"] == len(data)
    assert store.resolve(experiment_id="m6eval-x-1", name="evaluation_report.json") == (
        tmp_path / "m6eval-x-1" / "evaluation_report.json"
    )


def test_store_writes_text_and_binary_payloads(tmp_path) -> None:
    store = ExperimentArtifactStore(tmp_path)

    text = store.store(experiment_id="e", name="train.log", payload="line\n", kind="train_log")
    assert text["content_type"].startswith("text/plain")
    assert text["size"] == len(b"line\n")

    binary = store.store(experiment_id="e", name="model.bin", payload=b"\x00\x01", kind="model")
    assert binary["size"] == 2


def test_store_rejects_unsafe_components_and_oversize(tmp_path) -> None:
    store = ExperimentArtifactStore(tmp_path)

    with pytest.raises(ValueError):
        store.store(experiment_id="..", name="x.json", payload={})
    with pytest.raises(ValueError):
        store.store(experiment_id="e", name="///", payload={})
    with pytest.raises(ValueError):
        store.store(experiment_id="e", name="big.bin", payload=b"x" * (MAX_ARTIFACT_BYTES + 1))
