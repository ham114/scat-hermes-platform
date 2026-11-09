"""M5-10 对话侧物化能力测试：确认 ID 语义与 prepare/submit/status（单航班分段测试已迁至 test_eval_tools.py）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from agents import materialization_tools as tools
from agents import scat_tools
from agents.materialization_tools import (
    MaterializationJobConfirmationStore,
    PreparedMaterializationJob,
    _confirmation_scope,
    cancel_materialization_job_submission,
    get_materialization_job,
    list_materialization_jobs,
    prepare_materialization_job,
    submit_materialization_job,
)

BATCH = "scat20161015_20161021"
CONFIG: dict[str, Any] = {"configurable": {"thread_id": "t1", "user_id": "u1"}}


def _prepared(**overrides: Any) -> PreparedMaterializationJob:
    payload: dict[str, Any] = {
        "kind": "phase_segments",
        "batch": BATCH,
        "source_version": "scat-import-2",
        "algorithm_version": "1",
        "force": False,
        "limit": None,
        "total_count": 10,
        "pending_count": 8,
    }
    payload.update(overrides)
    return PreparedMaterializationJob(**payload)


def _patch_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """用假对象替代模块级 flight_database：不连真库，但提供 .db 与可等待的 connect()。

    注意 `_execute_service_operation` 取自 `agents.scat_tools`，它引用的是该模块自己的
    flight_database，因此两处都要替换。
    """
    fake = SimpleNamespace(db=Mock(name="flight_db"), connect=AsyncMock())
    monkeypatch.setattr(tools, "flight_database", fake)
    monkeypatch.setattr(scat_tools, "flight_database", fake)


def _patch_repositories(
    monkeypatch: pytest.MonkeyPatch,
    *,
    materialization: Mock | None = None,
    flight: Mock | None = None,
) -> Mock:
    repository = materialization or Mock()
    flight_repository = flight or Mock()
    monkeypatch.setattr(tools, "MaterializationRepository", lambda *a, **k: repository)
    monkeypatch.setattr(tools, "ScatFlightRepository", lambda *a, **k: flight_repository)
    _patch_database(monkeypatch)
    return repository


def test_confirmation_store_is_single_use_and_scope_bound():
    store = MaterializationJobConfirmationStore()
    prepared = _prepared()

    confirmation_id, expires_at = store.issue(prepared, thread_id="t1", user_id="u1")

    assert expires_at > datetime.now(UTC)
    assert store.consume(confirmation_id, thread_id="t2", user_id="u1") is None
    assert store.consume(confirmation_id, thread_id="t1", user_id="u2") is None
    assert store.consume(confirmation_id, thread_id="t1", user_id="u1") == prepared
    assert store.consume(confirmation_id, thread_id="t1", user_id="u1") is None


def test_confirmation_store_revoke_is_scope_bound():
    store = MaterializationJobConfirmationStore()
    confirmation_id, _ = store.issue(_prepared(), thread_id="t1", user_id="u1")

    assert store.revoke(confirmation_id, thread_id="t2", user_id="u1") is False
    assert store.revoke(confirmation_id, thread_id="t1", user_id="u1") is True
    assert store.consume(confirmation_id, thread_id="t1", user_id="u1") is None


def test_confirmation_store_expires_pending_entries():
    store = MaterializationJobConfirmationStore(ttl=timedelta(seconds=-1))

    confirmation_id, _ = store.issue(_prepared(), thread_id="t1", user_id="u1")

    assert store.consume(confirmation_id, thread_id="t1", user_id="u1") is None


def test_confirmation_scope_reads_thread_and_user():
    """确认 ID 的作用域取自 run config 的 configurable（thread_id/user_id）。"""
    assert _confirmation_scope({"configurable": {"thread_id": "t1", "user_id": "u1"}}) == (
        "t1",
        "u1",
    )
    assert _confirmation_scope({}) == (None, None)


@pytest.mark.asyncio
async def test_prepare_reports_impact_without_writing(monkeypatch: pytest.MonkeyPatch):
    store = MaterializationJobConfirmationStore()
    monkeypatch.setattr(tools, "confirmation_store", store)
    monkeypatch.setattr(tools, "_confirmation_scope", lambda config: ("t1", "u1"))
    materialization = Mock()
    materialization.existing_values = AsyncMock(return_value={"100001", "100002"})
    materialization.upsert_job = AsyncMock()
    flight = Mock()
    flight.count_all = AsyncMock(return_value=10)
    _patch_repositories(monkeypatch, materialization=materialization, flight=flight)

    result = await prepare_materialization_job.ainvoke(
        {"kind": "phase_segments", "batch": BATCH, "force": False},
        config=CONFIG,
    )

    assert result["status"] == "awaiting_confirmation"
    assert result["data"]["batch_flight_count"] == 10
    assert result["data"]["pending_flight_count"] == 8
    assert result["data"]["algorithm_version"] == "1"
    # prepare 只统计：不写任何作业
    materialization.upsert_job.assert_not_called()
    assert store.consume(result["confirmation_id"], thread_id="t1", user_id="u1") is not None


@pytest.mark.asyncio
async def test_prepare_schema_rejects_unknown_kind(monkeypatch: pytest.MonkeyPatch):
    _patch_database(monkeypatch)

    # 种类白名单在输入模型（Literal）就拦住，不会进入工具主体。
    with pytest.raises(ValidationError):
        await prepare_materialization_job.ainvoke(
            {"kind": "bogus_kind", "batch": BATCH},
            config=CONFIG,
        )


@pytest.mark.asyncio
async def test_submit_enqueues_pending_job_and_consumes_confirmation(
    monkeypatch: pytest.MonkeyPatch,
):
    store = MaterializationJobConfirmationStore()
    monkeypatch.setattr(tools, "confirmation_store", store)
    monkeypatch.setattr(tools, "_confirmation_scope", lambda config: ("t1", "u1"))
    upserts: list[Any] = []
    materialization = Mock()
    materialization.upsert_job = AsyncMock(side_effect=lambda record: upserts.append(record))
    _patch_repositories(monkeypatch, materialization=materialization)
    confirmation_id, _ = store.issue(_prepared(), thread_id="t1", user_id="u1")

    result = await submit_materialization_job.ainvoke(
        {"confirmation_id": confirmation_id},
        config=CONFIG,
    )

    assert result["status"] == "queued"
    assert result["data"]["job_status"] == "pending"
    assert result["data"]["pending_flight_count"] == 8
    record = upserts[0]
    assert record.kind == "phase_segments"
    assert record.status == "pending"
    assert record.total_count == 8
    assert record.progress == 0.0

    replay = await submit_materialization_job.ainvoke(
        {"confirmation_id": confirmation_id},
        config=CONFIG,
    )
    assert replay["status"] == "invalid_confirmation"
    assert len(upserts) == 1


@pytest.mark.asyncio
async def test_submit_rejects_unknown_confirmation(monkeypatch: pytest.MonkeyPatch):
    _patch_repositories(monkeypatch)

    result = await submit_materialization_job.ainvoke(
        {"confirmation_id": "deadbeefdeadbeef"},
        config=CONFIG,
    )

    assert result["status"] == "invalid_confirmation"


@pytest.mark.asyncio
async def test_cancel_prepared_materialization_revokes_confirmation(
    monkeypatch: pytest.MonkeyPatch,
):
    store = MaterializationJobConfirmationStore()
    monkeypatch.setattr(tools, "confirmation_store", store)
    monkeypatch.setattr(tools, "_confirmation_scope", lambda config: ("t1", "u1"))
    confirmation_id, _ = store.issue(_prepared(), thread_id="t1", user_id="u1")

    result = await cancel_materialization_job_submission.ainvoke(
        {"confirmation_id": confirmation_id},
        config=CONFIG,
    )

    assert result["status"] == "cancelled"
    assert store.consume(confirmation_id, thread_id="t1", user_id="u1") is None


@pytest.mark.asyncio
async def test_job_status_tool_delegates_to_service(monkeypatch: pytest.MonkeyPatch):
    service = Mock()
    service.get_job = AsyncMock(
        return_value={"status": "ok", "data": {"job_id": "job-1", "status": "running"}}
    )
    monkeypatch.setattr(tools, "MaterializationService", lambda *a, **k: service)
    _patch_database(monkeypatch)

    result = await get_materialization_job.ainvoke({"job_id": "job-12345678"}, config=CONFIG)

    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    assert inner.get("job_id") == "job-1"
    service.get_job.assert_awaited_once_with("job-12345678")


@pytest.mark.asyncio
async def test_job_list_tool_delegates_to_service(monkeypatch: pytest.MonkeyPatch):
    service = Mock()
    service.list_jobs = AsyncMock(
        return_value={"status": "ok", "data": {"returned": 1, "items": [{"job_id": "j1"}]}}
    )
    monkeypatch.setattr(tools, "MaterializationService", lambda *a, **k: service)
    _patch_database(monkeypatch)

    result = await list_materialization_jobs.ainvoke(
        {"kind": "phase_segments", "batch": BATCH, "status": "running", "limit": 10}
    )

    assert result["status"] == "ok"
    service.list_jobs.assert_awaited_once_with(
        kind="phase_segments",
        batch=BATCH,
        status_filter="running",
        limit=10,
    )
