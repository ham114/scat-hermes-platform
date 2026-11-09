"""Repository tests for bounded, owner-scoped analysis artifacts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analysis_runs import (
    AnalysisArtifactRecord,
    AnalysisArtifactRepository,
    AnalysisArtifactTooLargeError,
)
from core.settings import settings
from schema.analysis import AnalysisResult, MetricItem, MetricsBlock


def _artifact() -> AnalysisArtifactRecord:
    analysis = AnalysisResult(
        dataset="scat",
        operation="run_regression_model",
        title="Persisted prediction",
        blocks=[
            MetricsBlock(
                title="Summary",
                items=[MetricItem(key="point_count", label="Points", value=36)],
            )
        ],
    )
    return AnalysisArtifactRecord.build(
        artifact_id="a" * 32,
        run_id="b" * 32,
        owner_user_id="user-1",
        thread_id="thread-1",
        model_key="scat-gbar-v23",
        model_version="23",
        analysis_result=analysis,
        created_at=datetime(2026, 9, 16, tzinfo=UTC),
    )


class _Cursor:
    def __init__(self, documents):
        self.documents = documents
        self.limit_value = None

    def sort(self, *args):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    async def to_list(self, *, length):
        return self.documents[:length]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_multipage_preflight_initializes_cold_database_before_reading_indexes(
    monkeypatch, legacy
):
    from analysis_runs import artifact_repository as module

    connected = False

    async def connect():
        nonlocal connected
        connected = True

    collection = SimpleNamespace(
        index_information=AsyncMock(
            return_value={"old": {"unique": True, "key": [("run_id", 1), ("kind", 1)]}}
            if legacy
            else {}
        ),
        create_indexes=AsyncMock(),
    )

    def get_collection(name):
        assert connected, "Cold tool processes have no database until connect finishes"
        return collection

    monkeypatch.setattr(module.flight_database, "connect", connect)
    monkeypatch.setattr(module.flight_database, "get_collection", get_collection)
    repository = AnalysisArtifactRepository()
    if legacy:
        with pytest.raises(ValueError, match="legacy unique"):
            await repository.require_multipage()
        collection.create_indexes.assert_not_awaited()
    else:
        await repository.require_multipage()
        collection.create_indexes.assert_awaited_once()
    assert connected


@pytest.mark.asyncio
async def test_artifact_repository_creates_and_reads_with_owner_filter():
    artifact = _artifact()
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        insert_one=AsyncMock(),
        find_one=AsyncMock(return_value=artifact.to_mongo_document()),
    )
    repository = AnalysisArtifactRepository(collection=collection)

    await repository.create(artifact)
    restored = await repository.get_for_owner(
        artifact.artifact_id,
        owner_user_id="user-1",
    )

    assert restored == artifact
    collection.create_indexes.assert_awaited_once()
    indexes = collection.create_indexes.await_args.args[0]
    assert not next(
        i.document for i in indexes if i.document["name"] == "ix_analysis_artifact_run_kind"
    ).get("unique", False)
    collection.insert_one.assert_awaited_once_with(artifact.to_mongo_document())
    collection.find_one.assert_awaited_once_with(
        {"artifact_id": artifact.artifact_id, "owner_user_id": "user-1"}
    )


@pytest.mark.asyncio
async def test_artifact_repository_rejects_payload_over_configured_limit(monkeypatch):
    artifact = _artifact()
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        insert_one=AsyncMock(),
    )
    repository = AnalysisArtifactRepository(collection=collection)
    monkeypatch.setattr(settings, "ANALYSIS_ARTIFACT_MAX_BYTES", 1)

    with pytest.raises(AnalysisArtifactTooLargeError):
        await repository.create(artifact)

    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_repository_lists_recent_owner_metadata():
    artifact = _artifact()
    cursor = _Cursor([artifact.to_mongo_document()])
    collection = SimpleNamespace(create_indexes=AsyncMock(), find=lambda query: cursor)
    repository = AnalysisArtifactRepository(collection=collection)

    restored = await repository.list_for_owner(
        owner_user_id="user-1",
        model_key="scat-gbar-v23",
        limit=10,
    )

    assert restored == [artifact]
    assert cursor.limit_value == 10


@pytest.mark.asyncio
async def test_legacy_unique_index_rejects_gpu_reports_without_automatic_migration():
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        index_information=AsyncMock(
            return_value={
                "uq_analysis_artifact_run_kind": {
                    "unique": True,
                    "key": [("run_id", 1), ("kind", 1)],
                }
            }
        ),
    )
    with pytest.raises(ValueError, match="multipage"):
        await AnalysisArtifactRepository(collection=collection).require_multipage()


@pytest.mark.asyncio
async def test_new_collection_supports_multiple_artifact_ids_for_one_run():
    collection = SimpleNamespace(
        create_indexes=AsyncMock(),
        index_information=AsyncMock(
            return_value={"uq_analysis_artifact_id": {"unique": True, "key": [("artifact_id", 1)]}}
        ),
    )
    await AnalysisArtifactRepository(collection=collection).require_multipage()
