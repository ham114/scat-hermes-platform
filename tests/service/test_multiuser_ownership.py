"""M12: two authenticated users cannot read or resume each other's work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from core import settings
from core.identity_scope import owner_filter, request_owner
from service.ownership import OwnershipRepository, authorize_thread, ownership_repository


class Collection:
    def __init__(self):
        self.records = {}

    async def find_one(self, query):
        return self.records.get(query["_id"])

    async def update_one(self, query, update, upsert=False):
        self.records.setdefault(query["_id"], update["$setOnInsert"])


@pytest.fixture
def identities(monkeypatch):
    monkeypatch.setattr(settings, "ANALYSIS_IDENTITY_MODE", "trusted_proxy")
    monkeypatch.setattr(settings, "ANALYSIS_LEGACY_OWNER_ID", "legacy-admin")
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("internal-test-only"))
    monkeypatch.setattr(ownership_repository, "_collection", Collection())


def headers(owner):
    return {"Authorization": "Bearer internal-test-only", "X-Authenticated-User": owner}


@pytest.mark.parametrize("path", ["/invoke", "/stream", "/history", "/scat-flight-agent/history"])
def test_cross_user_thread_denied_before_execution(path, test_client, mock_agent, identities):
    ownership_repository._collection.records["thread:alice-thread"] = {"owner_user_id": "alice"}
    response = test_client.post(
        path, headers=headers("bob"),
        json={"message": "hello", "thread_id": "alice-thread", "user_id": "alice"},
    )
    assert response.status_code == 404
    mock_agent.ainvoke.assert_not_awaited()


def test_proxy_subject_overrides_body_identity(test_client, mock_agent, identities):
    mock_agent.aget_state.return_value = SimpleNamespace(values={}, tasks=[])
    result = test_client.post("/invoke", headers=headers("alice"),
                              json={"message": "hello", "thread_id": "new-thread", "user_id": "bob"})
    assert result.status_code == 200
    assert mock_agent.ainvoke.await_args.kwargs["config"]["configurable"]["user_id"] == "alice"
    assert ownership_repository._collection.records["thread:new-thread"]["owner_user_id"] == "alice"


def test_missing_identity_or_bearer_denied(test_client, identities):
    assert test_client.post("/history", json={"thread_id": "any"}).status_code == 401
    assert test_client.post("/history", headers={"X-Authenticated-User": "alice"},
                            json={"thread_id": "any"}).status_code == 401


def test_feedback_other_user_denied(test_client, identities):
    result = test_client.post("/feedback", headers=headers("bob"),
        json={"run_id": "00000000-0000-0000-0000-000000000001", "key": "score", "score": 1})
    assert result.status_code == 404


def test_agui_other_user_denied(test_client, identities, monkeypatch):
    graph = AsyncMock()
    monkeypatch.setattr("service.agui.get_agent", lambda _: graph)
    ownership_repository._collection.records["thread:alice-thread"] = {"owner_user_id": "alice"}
    result = test_client.post("/agui/run", headers=headers("bob"), json={
        "threadId": "alice-thread", "runId": "run", "messages": [], "tools": [],
        "context": [], "state": {}, "forwardedProps": {"configurable": {"user_id": "alice"}},
    })
    assert result.status_code == 404
    graph.aget_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_historical_thread_cannot_be_claimed_by_new_user(identities):
    graph = AsyncMock()
    graph.aget_state.return_value = SimpleNamespace(values={"messages": ["old"]})
    token = request_owner.set("bob")
    try:
        with pytest.raises(HTTPException) as error:
            await authorize_thread("old-thread", graph, create=True)
        assert error.value.status_code == 404
        assert "thread:old-thread" not in ownership_repository._collection.records
    finally:
        request_owner.reset(token)
    token = request_owner.set("legacy-admin")
    try:
        await authorize_thread("old-thread", graph, create=False)
    finally:
        request_owner.reset(token)


@pytest.mark.asyncio
async def test_claim_cannot_change_owner():
    repo = OwnershipRepository(Collection())
    await repo.claim("thread", "same", "alice")
    with pytest.raises(HTTPException):
        await repo.claim("thread", "same", "bob")
    assert (await repo.get("thread", "same"))["owner_user_id"] == "alice"


def test_personal_task_filters_and_worker_scope(identities):
    assert owner_filter() == {}
    token = request_owner.set("alice")
    try:
        assert owner_filter() == {"owner_user_id": "alice"}
    finally:
        request_owner.reset(token)
    token = request_owner.set("legacy-admin")
    try:
        assert owner_filter() == {"$or": [{"owner_user_id": "legacy-admin"}, {"owner_user_id": None}]}
    finally:
        request_owner.reset(token)
