from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from threading import Condition, Event, Thread
from types import SimpleNamespace

import httpx
import pytest

from application.knowledge import KnowledgeSearchService
from rag_retrieval.production import build_handler


@pytest.fixture
def service():
    engine = SimpleNamespace(
        failed=False, identity={"status": "ready", "profile_id": "m8_rrf_qwen06b_top100"}
    )
    calls = []

    def search(query, limit):
        calls.append((query, limit))
        return [
            {
                "rank": i + 1,
                "source_id": "source",
                "source_version": "v4",
                "chunk_id": str(i),
                "text": "evidence",
                "score": 0.9,
            }
            for i in range(limit)
        ]

    engine.search = search
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(engine))
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield engine, calls, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize("limit", [1, 2, 5, 10])
def test_legacy_search_contract_preserves_requested_limit_and_citations(service, limit):
    _engine, calls, url = service
    with httpx.Client(trust_env=False) as client:
        response = client.post(url + "/search", json={"query": "  question  ", "limit": limit})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok" and data["count"] == len(data["results"]) == limit
    assert all(
        row["source_id"] and row["source_version"] and row["score"] for row in data["results"]
    )
    assert calls == [("question", limit)]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"query": " "},
        {"query": "q", "limit": True},
        {"query": "q", "limit": 11},
        {"query": "q", "path": "/etc/passwd"},
        {"query": "x" * 1001},
    ],
)
def test_invalid_input_never_reaches_gpu(service, payload):
    _engine, calls, url = service
    with httpx.Client(trust_env=False) as client:
        assert client.post(url + "/search", json=payload).status_code == 400
    assert not calls


def test_gpu_failure_is_unavailable_and_readiness_drops(service):
    engine, _calls, url = service

    def fail(*args):
        raise RuntimeError("private implementation detail")

    engine.search = fail
    with httpx.Client(trust_env=False) as client:
        response = client.post(url + "/search", json={"query": "q"})
        assert response.status_code == 503
        assert "private implementation detail" not in response.text
        assert client.get(url + "/health/ready").status_code == 503


def test_admission_rejects_third_gpu_request_without_an_unbounded_queue(service):
    engine, _calls, url = service
    condition, release = Condition(), Event()
    count = 0

    def blocked(*args):
        nonlocal count
        with condition:
            count += 1
            condition.notify_all()
        release.wait(5)
        return []

    engine.search = blocked

    def request():
        with httpx.Client(trust_env=False, timeout=8) as client:
            return client.post(url + "/search", json={"query": "q"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(request) for _ in range(2)]
        try:
            with condition:
                assert condition.wait_for(lambda: count == 2, timeout=4)
            response = request()
            assert response.status_code == 429 and response.headers["Retry-After"] == "2"
        finally:
            release.set()
        assert all(future.result().status_code == 200 for future in pending)


@pytest.mark.asyncio
async def test_existing_knowledge_operation_reads_the_production_payload(service):
    _engine, _calls, url = service
    client = KnowledgeSearchService(enabled=True, base_url=url, timeout=3)
    result = await client.search("definition", limit=2)
    assert result["status"] == "ok"
    assert result["data"]["profile_id"] == "m8_rrf_qwen06b_top100"
    assert len(result["data"]["results"]) == 2
