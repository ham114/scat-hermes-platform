import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from application.context import ExecutionContext
from hermes_runtime.client import HermesHTTPRuntime, HermesRuntimeError
from hermes_runtime.registry import RunBindingConflictError, RunNotFoundError, RunRegistry

KEY = SecretStr("synthetic-service-key-123456789")
RUN = "run_synthetic001"


@pytest.fixture
def context():
    return ExecutionContext("researcher", "thread-001")


@pytest.fixture
def registry(tmp_path):
    return RunRegistry(tmp_path / "runs.db", workspace_root=tmp_path)


def runtime(registry, handler):
    return HermesHTTPRuntime(
        "http://127.0.0.1:18081", KEY, registry, transport=httpx.MockTransport(handler)
    )


def test_context_scope_is_stable_and_distinguishes_user_and_thread(context):
    assert context.scope_key == ExecutionContext("researcher", "thread-001").scope_key
    assert context.scope_key != ExecutionContext("another", "thread-001").scope_key
    assert context.scope_key != ExecutionContext("researcher", "thread-002").scope_key
    assert "researcher" not in context.scope_key


@pytest.mark.parametrize(
    "user,thread",
    [("", "thread"), ("a\nb", "thread"), ("user", "../other"), ("user", "t\r\nHeader:x")],
)
def test_context_rejects_unsafe_identity(user, thread):
    with pytest.raises(ValueError):
        ExecutionContext(user, thread)


def test_registry_is_durable_and_owner_scoped(registry, context):
    request = str(uuid4())
    registry.bind(context, request, RUN)
    reopened = RunRegistry(registry.path, workspace_root=registry.path.parent)
    reopened.require(context, RUN)
    for other in [
        ExecutionContext("other", context.thread_id),
        ExecutionContext(context.user_id, "different"),
    ]:
        with pytest.raises(RunNotFoundError):
            reopened.require(other, RUN)
    with pytest.raises(RunNotFoundError):
        reopened.require(context, "../run_other")
    registry.bind(context, request, RUN)
    with pytest.raises(RunBindingConflictError):
        registry.bind(context, request, "run_changed")
    with pytest.raises(RunBindingConflictError):
        registry.bind(ExecutionContext("other", "thread-001"), str(uuid4()), RUN)


@pytest.mark.asyncio
async def test_create_status_stop_follow_pinned_routes(registry, context):
    calls = []
    request_id = uuid4()

    def respond(request):
        calls.append(request)
        if request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": RUN, "status": "started", "replayed": False})
        status = "stopping" if request.method == "POST" else "running"
        return httpx.Response(200, json={"run_id": RUN, "status": status})

    async with runtime(registry, respond) as client:
        await client.create_run(context, "synthetic question", request_id=request_id)
        assert (await client.get_run(context, RUN))["status"] == "running"
        assert (await client.stop_run(context, RUN))["status"] == "stopping"
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", "/v1/runs"),
        ("GET", f"/v1/runs/{RUN}"),
        ("POST", f"/v1/runs/{RUN}/stop"),
    ]
    assert json.loads(calls[0].content) == {"input": "synthetic question"}
    assert calls[0].headers["Idempotency-Key"] == context.scope_key + ":" + str(request_id)
    for call in calls:
        assert call.headers["X-Hermes-Session-Key"] == context.scope_key
        assert call.headers["Authorization"] == "Bearer " + KEY.get_secret_value()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "stop", "events"])
async def test_cross_user_and_cross_thread_refused_before_http(registry, context, operation):
    registry.bind(context, str(uuid4()), RUN)

    def should_not_call(_):
        raise AssertionError("An unauthorized request reached upstream")

    async with runtime(registry, should_not_call) as client:
        for other in [
            ExecutionContext("other", "thread-001"),
            ExecutionContext("researcher", "another"),
        ]:
            with pytest.raises(RunNotFoundError):
                if operation == "get":
                    await client.get_run(other, RUN)
                elif operation == "stop":
                    await client.stop_run(other, RUN)
                else:
                    _ = [event async for event in client.events(other, RUN)]


@pytest.mark.asyncio
async def test_admission_timeout_is_unknown_and_never_retried(registry, context):
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-bearing upstream error", request=request)

    async with runtime(registry, fail) as client:
        with pytest.raises(HermesRuntimeError) as error:
            await client.create_run(context, "synthetic", request_id=uuid4())
    assert len(calls) == 1
    assert error.value.outcome_unknown is True
    assert "secret-bearing" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "upstream_auth_failed"),
        (403, "upstream_forbidden"),
        (404, "upstream_not_found"),
        (409, "upstream_conflict"),
        (429, "upstream_rate_limited"),
        (503, "upstream_failed"),
        (302, "upstream_redirect_refused"),
    ],
)
async def test_errors_and_redirects_do_not_leak_remote_body(registry, context, status, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, text="secret-not-for-clients", headers={"Location": "https://other.invalid"}
        )

    async with runtime(registry, respond) as client:
        with pytest.raises(HermesRuntimeError) as error:
            await client.create_run(context, "synthetic", request_id=uuid4())
    assert error.value.code == code
    assert "secret-not-for-clients" not in str(error.value)
    assert len(calls) == 1


class Chunks(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload, self.closed = payload, False

    async def __aiter__(self):
        raw = self.payload.encode()
        for offset in range(0, len(raw), 7):
            yield raw[offset : offset + 7]

    async def aclose(self):
        self.closed = True


def frame(name, seq, **fields):
    data = {"event": name, "run_id": RUN, "seq": seq, **fields}
    return f"id: {seq}\r\ndata: {json.dumps(data, ensure_ascii=False)}\r\n\r\n"


@pytest.mark.asyncio
async def test_stream_handles_chunked_chinese_keepalive_replay_and_terminal(registry, context):
    registry.bind(context, str(uuid4()), RUN)
    chunks = Chunks(
        ": open\r\n\r\n"
        + frame("run.started", 0)
        + frame("text.delta", 1, text="合成测试")
        + frame("run.completed", 2, completed=True)
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=chunks
        )

    async with runtime(registry, respond) as client:
        events = [event async for event in client.events(context, RUN, last_sequence=0)]
    assert [e.name for e in events] == ["text.delta", "run.completed"]
    assert events[0].payload["text"] == "合成测试"
    assert requests[0].headers["Last-Event-ID"] == "0"
    assert chunks.closed


@pytest.mark.asyncio
async def test_disconnect_is_not_completed(registry, context):
    registry.bind(context, str(uuid4()), RUN)
    chunks = Chunks(frame("run.started", 0))
    async with runtime(
        registry,
        lambda _: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=chunks),
    ) as client:
        with pytest.raises(HermesRuntimeError, match="event_stream_interrupted"):
            _ = [event async for event in client.events(context, RUN)]
    assert chunks.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,code",
    [
        ("data: not-json\n\n", "invalid_event_json"),
        ('data: {"event":"run.started","run_id":"run_other"}\n\n', "upstream_run_mismatch"),
        (
            'id: 5\ndata: {"event":"run.started","run_id":"run_synthetic001","seq":3}\n\n',
            "invalid_event_sequence",
        ),
        (frame("run.completed", 0, completed=False), "inconsistent_completion"),
        ("data: " + ("x" * 262145) + "\n\n", "event_frame_too_large"),
    ],
    ids=[
        "invalid-json",
        "other-run",
        "cursor-mismatch",
        "inconsistent-completion",
        "oversize-frame",
    ],
)
async def test_malformed_or_mismatched_events_rejected(registry, context, payload, code):
    registry.bind(context, str(uuid4()), RUN)
    async with runtime(
        registry,
        lambda _: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Chunks(payload)
        ),
    ) as client:
        with pytest.raises(HermesRuntimeError) as error:
            _ = [event async for event in client.events(context, RUN)]
    assert error.value.code == code


@pytest.mark.parametrize(
    "url",
    [
        "https://key:secret@example.test",
        "https://example.test/path",
        "http://public.example.test",
        "https://example.test?key=secret",
    ],
)
def test_service_url_rejects_credential_urls_paths_and_plain_public_http(registry, url):
    with pytest.raises(ValueError):
        HermesHTTPRuntime(url, KEY, registry)


def test_registry_refuses_outside_workspace_without_writing(tmp_path):
    target = tmp_path.parent / "outside-registry.db"
    with pytest.raises(ValueError, match="inside its workspace"):
        RunRegistry(target, workspace_root=tmp_path)
    assert not target.exists()
