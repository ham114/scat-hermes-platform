"""M4-10 维护测试：索引创建、旧版本清理与可重建删除。"""

from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import OperationFailure

from scat.materialization.contracts import (
    MATERIALIZATION_BUSINESS_KEYS,
    MATERIALIZATION_COLLECTIONS,
    MATERIALIZATION_TTL_INDEXES,
    MATERIALIZATION_VERSION_KEYS,
)
from scat.materialization.maintenance import (
    MATERIALIZED_KINDS,
    cleanup_stale_versions,
    purge_batch_version,
)
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import FLIGHT_FEATURES_ALGORITHM_VERSION

BATCH = "scat20161015_20161021"


class _DeleteResult:
    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count


class FakeCollection:
    """记录过滤条件的假集合：dry-run 只 count，apply 走 delete_many。

    `conflict_on_ttl=True` 模拟历史遗留的同 key 普通索引：首次建 TTL 时报
    IndexOptionsConflict（code 85），删除旧索引后重建成功。
    """

    def __init__(
        self,
        *,
        matched: int = 0,
        deleted: int = 0,
        conflict_on_ttl: bool = False,
        existing_indexes: dict[str, Any] | None = None,
    ) -> None:
        self.matched = matched
        self.deleted = deleted
        self.conflict_on_ttl = conflict_on_ttl
        self.existing_indexes = dict(existing_indexes or {})
        self.count_filters: list[dict[str, Any]] = []
        self.delete_filters: list[dict[str, Any]] = []
        self.index_calls: list[tuple[Any, dict[str, Any]]] = []
        self.index_names: list[str] = []
        self.dropped_indexes: list[str] = []

    async def count_documents(self, filters: dict[str, Any]) -> int:
        self.count_filters.append(filters)
        return self.matched

    async def delete_many(self, filters: dict[str, Any]) -> _DeleteResult:
        self.delete_filters.append(filters)
        return _DeleteResult(self.deleted)

    async def index_information(self) -> dict[str, Any]:
        return dict(self.existing_indexes)

    async def drop_index(self, name: str) -> None:
        self.dropped_indexes.append(name)
        self.existing_indexes.pop(name, None)

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        is_ttl = kwargs.get("expireAfterSeconds") is not None
        if is_ttl and self.conflict_on_ttl and not self.dropped_indexes:
            raise OperationFailure("equivalent index already exists", 85)
        self.index_calls.append((keys, kwargs))
        name = kwargs.get("name") or f"index_{len(self.index_calls)}"
        self.index_names.append(name)
        return name


class FakeRepository:
    def __init__(self, collections: dict[str, FakeCollection]) -> None:
        self._collections = collections

    def __getitem__(self, name: str) -> FakeCollection:
        # MaterializationRepository 通过 database[name] 取集合
        return self._collections.setdefault(name, FakeCollection())

    def collection(self, kind: str) -> FakeCollection:
        return self._collections.setdefault(kind, FakeCollection())


def _repository(
    *, matched: int = 0, deleted: int = 0
) -> tuple[FakeRepository, dict[str, FakeCollection]]:
    collections = {
        kind: FakeCollection(matched=matched, deleted=deleted) for kind in MATERIALIZED_KINDS
    }
    return FakeRepository(collections), collections


@pytest.mark.asyncio
async def test_cleanup_dry_run_counts_without_deleting():
    repository, collections = _repository(matched=3)

    report = await cleanup_stale_versions(repository, batch=BATCH)

    assert report["dry_run"] is True
    assert report["deleted_total"] == 0
    assert {item["kind"] for item in report["results"]} == set(MATERIALIZED_KINDS)
    for kind, collection in collections.items():
        assert collection.delete_filters == []
        filters = collection.count_filters[0]
        assert filters["batch"] == BATCH
        assert {"source_version", "algorithm_version"} == {
            key for clause in filters["$or"] for key in clause
        }
        assert (
            report["results"][[item["kind"] for item in report["results"]].index(kind)]["matched"]
            == 3
        )


@pytest.mark.asyncio
async def test_cleanup_apply_deletes_only_stale_versions():
    repository, collections = _repository(deleted=2)

    report = await cleanup_stale_versions(repository, batch=BATCH, dry_run=False)

    assert report["dry_run"] is False
    assert report["deleted_total"] == 2 * len(MATERIALIZED_KINDS)
    for collection in collections.values():
        assert collection.count_filters == []
        assert len(collection.delete_filters) == 1


@pytest.mark.asyncio
async def test_cleanup_can_target_single_kind():
    repository, collections = _repository(matched=1)

    report = await cleanup_stale_versions(repository, kind="flight_features")

    assert [item["kind"] for item in report["results"]] == ["flight_features"]
    assert collections["batch_statistics"].count_filters == []
    assert "batch" not in collections["flight_features"].count_filters[0]


@pytest.mark.asyncio
async def test_cleanup_rejects_unknown_kind():
    repository, _ = _repository()

    with pytest.raises(ValueError, match="未知物化集合种类"):
        await cleanup_stale_versions(repository, kind="jobs")


@pytest.mark.asyncio
async def test_purge_batch_version_filters_by_batch_and_versions():
    repository, collections = _repository(deleted=7)

    deleted = await purge_batch_version(repository, kind="flight_features", batch=BATCH)

    assert deleted == 7
    filters = collections["flight_features"].delete_filters[0]
    assert filters == {
        "batch": BATCH,
        "source_version": "scat-import-2",
        "algorithm_version": FLIGHT_FEATURES_ALGORITHM_VERSION,
    }


@pytest.mark.asyncio
async def test_purge_batch_version_rejects_unknown_kind():
    repository, _ = _repository()

    with pytest.raises(ValueError, match="未知物化集合种类"):
        await purge_batch_version(repository, kind="jobs", batch=BATCH)


@pytest.mark.asyncio
async def test_ensure_indexes_declares_unique_query_and_ttl_indexes():
    collections: dict[str, FakeCollection] = {}
    repository = MaterializationRepository(FakeRepository(collections))

    created = await repository.ensure_indexes()

    assert len(created) == len(set(created))
    for kind, collection_name in MATERIALIZATION_COLLECTIONS.items():
        collection = collections[collection_name]
        unique_call = next(call for call in collection.index_calls if call[1].get("unique"))
        expected_keys = [
            *MATERIALIZATION_BUSINESS_KEYS[kind],
            *MATERIALIZATION_VERSION_KEYS,
        ]
        assert [field for field, _ in unique_call[0]] == expected_keys
        assert unique_call[1]["name"] == f"uq_{kind}_versioned"
    ttl_call = next(
        call
        for call in collections[MATERIALIZATION_COLLECTIONS["jobs"]].index_calls
        if call[1].get("expireAfterSeconds") == 0
    )
    assert ttl_call[0] == [("expires_at", 1)]
    # TTL 字段不得再建普通索引（同一 key 不能同时存在普通索引与 TTL 索引）
    assert "idx_jobs_expires_at" not in collections[MATERIALIZATION_COLLECTIONS["jobs"]].index_names


@pytest.mark.asyncio
async def test_ensure_indexes_repairs_conflicting_non_ttl_index():
    legacy_index = {"idx_jobs_expires_at": {"key": [("expires_at", 1)]}}
    collections: dict[str, FakeCollection] = {
        MATERIALIZATION_COLLECTIONS["jobs"]: FakeCollection(
            conflict_on_ttl=True,
            existing_indexes=legacy_index,
        )
    }
    repository = MaterializationRepository(FakeRepository(collections))

    created = await repository.ensure_indexes()

    jobs = collections[MATERIALIZATION_COLLECTIONS["jobs"]]
    assert jobs.dropped_indexes == ["idx_jobs_expires_at"]
    assert f"ttl_jobs_{MATERIALIZATION_TTL_INDEXES['jobs'][0]}" in created
    ttl_call = next(call for call in jobs.index_calls if call[1].get("expireAfterSeconds") == 0)
    assert ttl_call[0] == [("expires_at", 1)]
