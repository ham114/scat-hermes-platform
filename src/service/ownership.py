"""Authenticated request scope and persistent ownership of graph threads/runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig

from core import settings
from core.identity_scope import current_owner, request_owner


class OwnershipRepository:
    def __init__(self, collection: Any = None):
        self._collection = collection

    async def collection(self):
        if self._collection is not None:
            return self._collection
        from scat.infrastructure.database import flight_database

        await flight_database.connect()
        return flight_database.get_collection("analysis_access_owners")

    async def get(self, kind: str, identifier: str) -> dict | None:
        return await (await self.collection()).find_one({"_id": kind + ":" + identifier})

    async def claim(self, kind: str, identifier: str, owner: str) -> None:
        from pymongo.errors import DuplicateKeyError

        collection = await self.collection()
        key = kind + ":" + identifier
        try:
            await collection.update_one(
                {"_id": key},
                {"$setOnInsert": {"owner_user_id": owner, "created_at": datetime.now(UTC)}},
                upsert=True,
            )
        except DuplicateKeyError:
            # A concurrent claim won the unique _id race. Re-read its owner.
            pass
        record = await collection.find_one({"_id": key})
        if record is None or record["owner_user_id"] != owner:
            raise HTTPException(404, "Resource not found")


ownership_repository = OwnershipRepository()


async def authorize_thread(thread_id: str, graph: Any, *, create: bool) -> None:
    owner = current_owner()
    if owner is None:
        return
    if not isinstance(thread_id, str) or not (1 <= len(thread_id) <= 200):
        raise HTTPException(422, "Invalid thread identifier")
    if any(ord(c) < 32 or ord(c) == 127 for c in thread_id):
        raise HTTPException(422, "Invalid thread identifier")
    record = await ownership_repository.get("thread", thread_id)
    if record is not None:
        if record["owner_user_id"] != owner:
            raise HTTPException(404, "Resource not found")
        return
    # Never let the first visitor claim an existing historical checkpoint.
    snapshot = await graph.aget_state(RunnableConfig(configurable={"thread_id": thread_id}))
    historical = bool(snapshot.values)
    if historical and owner != settings.ANALYSIS_LEGACY_OWNER_ID:
        raise HTTPException(404, "Resource not found")
    if not historical and not create:
        raise HTTPException(404, "Resource not found")
    await ownership_repository.claim("thread", thread_id, owner)


async def register_request_run(run_id: str) -> None:
    owner = current_owner()
    if owner is not None:
        await ownership_repository.claim("request_run", run_id, owner)


async def authorize_request_run(run_id: str) -> None:
    owner = current_owner()
    if owner is None:
        return
    record = await ownership_repository.get("request_run", run_id)
    if record is None or record["owner_user_id"] != owner:
        raise HTTPException(404, "Resource not found")


class TrustedIdentityMiddleware:
    """Bind a verified proxy subject for the full HTTP/SSE request lifetime.

    The production listener is loopback-only. The Hermes portal verifies short-lived
    method/target identity signatures; legacy profiles retain their configured boundary.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or settings.ANALYSIS_IDENTITY_MODE != "trusted_proxy"
            or scope.get("path", "").startswith("/health")
        ):
            await self.app(scope, receive, send)
            return
        from fastapi import Request
        from fastapi.responses import JSONResponse

        from service.identity import get_analysis_owner_id

        headers = dict(scope.get("headers", []))
        raw = headers.get(b"x-authenticated-user", b"")
        try:
            owner = get_analysis_owner_id(raw.decode("utf-8"), Request(scope))
        except (HTTPException, UnicodeDecodeError):
            await JSONResponse({"detail": "Authenticated identity required"}, status_code=401)(
                scope, receive, send
            )
            return
        token = request_owner.set(owner)
        try:
            await self.app(scope, receive, send)
        finally:
            request_owner.reset(token)
