"""Contract tests for complete structured analysis artifacts."""

from datetime import UTC, datetime

from analysis_runs import AnalysisArtifactRecord
from schema.analysis import AnalysisResult, MetricItem, MetricsBlock


def _analysis() -> AnalysisResult:
    return AnalysisResult(
        dataset="scat",
        operation="run_regression_model",
        title="Persisted prediction",
        context={"external_id": "100002"},
        blocks=[
            MetricsBlock(
                title="Summary",
                items=[MetricItem(key="point_count", label="Points", value=36)],
            )
        ],
    )


def test_artifact_builds_stable_size_checksum_and_round_trips_bson_time():
    created_at = datetime(2026, 9, 16, tzinfo=UTC)
    artifact = AnalysisArtifactRecord.build(
        artifact_id="a" * 32,
        run_id="b" * 32,
        owner_user_id="user-1",
        thread_id="thread-1",
        model_key="scat-gbar-v23",
        model_version="23",
        analysis_result=_analysis(),
        created_at=created_at,
    )
    same = AnalysisArtifactRecord.build(
        artifact_id="c" * 32,
        run_id="d" * 32,
        owner_user_id="user-2",
        thread_id="thread-2",
        model_key="scat-gbar-v23",
        model_version="23",
        analysis_result=_analysis(),
        created_at=created_at,
    )

    assert artifact.payload_size_bytes > 0
    assert artifact.payload_sha256 == same.payload_sha256
    document = artifact.to_mongo_document()
    document["created_at"] = document["created_at"].replace(tzinfo=None)
    restored = AnalysisArtifactRecord.from_mongo_document(document)
    assert restored == artifact


def test_semantic_artifact_id_contains_readable_segments():
    from datetime import UTC, datetime

    from analysis_runs.artifact_contracts import (
        ARTIFACT_ID_PATTERN,
        build_semantic_artifact_id,
    )

    artifact_id = build_semantic_artifact_id(
        task="regression",
        model_key="scat-gbar-v23",
        dataset="scat",
        batch="scat20161015_20161021",
        external_id="100015",
        horizon_s=360.0,
        n_points=36,
        now=datetime(2026, 9, 17, 10, 30, 45, tzinfo=UTC),
        suffix="a3f2c1",
    )
    assert artifact_id == (
        "regression-scat-gbar-v23-scat-scat20161015_20161021-100015"
        "-h360-n36-20260917T103045-a3f2c1"
    )


def test_semantic_artifact_id_sanitizes_and_truncates_segments():
    import re
    from datetime import UTC, datetime

    from analysis_runs.artifact_contracts import (
        ARTIFACT_ID_PATTERN,
        build_semantic_artifact_id,
    )

    artifact_id = build_semantic_artifact_id(
        task="reg ression/任务",
        model_key="A" * 100,
        dataset="scat",
        batch=None,
        external_id="100015:extra",
        horizon_s=359.6,
        n_points=36,
        now=datetime(2026, 9, 17, 0, 0, 0, tzinfo=UTC),
        suffix="zz",
    )
    assert re.fullmatch(ARTIFACT_ID_PATTERN, artifact_id)
    assert len(artifact_id) <= 200
    assert "/" not in artifact_id and " " not in artifact_id


def test_legacy_hex_artifact_id_still_valid():
    import re

    from analysis_runs.artifact_contracts import ARTIFACT_ID_PATTERN

    assert re.fullmatch(ARTIFACT_ID_PATTERN, "990f20efd52642dea06a0bd9075a97f4")
    assert not re.fullmatch(ARTIFACT_ID_PATTERN, "short")
    assert not re.fullmatch(ARTIFACT_ID_PATTERN, "包含中文的id")
