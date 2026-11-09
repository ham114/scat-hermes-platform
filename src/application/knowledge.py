"""Framework-independent client for the existing read-only RAG service."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class KnowledgeSearchService:
    def __init__(
        self, *, enabled: bool, base_url: str, timeout: float = 10, http_client_factory=None
    ):
        self.enabled, self.base_url, self.timeout = enabled, base_url, timeout
        self.http_client_factory = http_client_factory

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        value = KnowledgeSearchInput(query=query, limit=limit)
        if not self.enabled:
            return {
                "status": "unavailable",
                "operation": "search_scat_knowledge",
                "message": "SCAT 知识检索基线未启用。",
                "data": None,
            }
        factory = self.http_client_factory or httpx.AsyncClient
        try:
            async with factory(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    self.base_url.rstrip("/") + "/search", json=value.model_dump()
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("retrieval_failed error_type=%s", type(exc).__name__)
            return {
                "status": "retrieval_error",
                "operation": "search_scat_knowledge",
                "message": "知识检索服务暂时不可用，请稍后重试。",
                "data": None,
            }
        return {
            "status": "ok",
            "operation": "search_scat_knowledge",
            "query": value.query,
            "data": payload,
        }
