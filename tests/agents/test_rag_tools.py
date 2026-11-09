from __future__ import annotations

import httpx
import pytest

from agents import rag_tools as module


@pytest.mark.asyncio
async def test_search_scat_knowledge_calls_loopback_service(monkeypatch) -> None:
    monkeypatch.setattr(module.settings, "SCAT_RAG_BASELINE_ENABLED", True)
    monkeypatch.setattr(module.settings, "SCAT_RAG_BASELINE_URL", "http://127.0.0.1:8091")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={"status": "ok", "profile_id": "m8_rrf", "results": []},
        )

    original = httpx.AsyncClient

    class ClientFactory:
        def __new__(cls, *args, **kwargs):
            return original(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(module.httpx, "AsyncClient", ClientFactory)
    result = await module.search_scat_knowledge.ainvoke({"query": "GBAR", "limit": 3})

    assert result["status"] == "ok"
    assert result["data"]["profile_id"] == "m8_rrf"


@pytest.mark.asyncio
async def test_search_scat_knowledge_is_explicitly_unavailable_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(module.settings, "SCAT_RAG_BASELINE_ENABLED", False)

    result = await module.search_scat_knowledge.ainvoke({"query": "GBAR"})

    assert result["status"] == "unavailable"
