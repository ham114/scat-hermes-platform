"""End-to-end orchestration and delivery gates with fake network/container boundaries."""

import asyncio
import hashlib
import json
import struct
import zlib

import httpx
import pytest

from application.context import ExecutionContext
from application.visualization_tasks import VisualizationTasks
from hermes_runtime.results import ToolResultStore
from visualization_runtime.launcher import RenderLauncher
from visualization_runtime.sandbox import SIZES, RenderError
from visualization_runtime.settings import VisualizationSettings
from visualization_runtime.snapshots import SnapshotStore, digest
from visualization_runtime.store import JobNotFound, VisualizationJobStore
from visualization_runtime.worker import VisualizationWorker


def png(viewport):
    width, height = SIZES[viewport]

    def chunk(kind, body):
        return (
            struct.pack("!I", len(body))
            + kind
            + body
            + struct.pack("!I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    rows = [
        (b"\0" + (b"\x25\x63\xeb" if y > height // 2 else b"\xff\xff\xff") * width)
        for y in range(height)
    ]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


class FakeSandbox:
    image = "sha256:" + "1" * 64

    def __init__(self):
        self.calls = 0
        self.removed = []

    def stopped(self, name):
        return True

    def remove(self, name):
        self.removed.append(name)

    def render(self, name, directory, request):
        self.calls += 1
        if "syntax_bad" in request["source"]:
            raise RenderError("render_execution_failed")
        output = directory / "output"
        output.mkdir(parents=True)
        images = []
        for viewport in request["viewports"]:
            raw = png(viewport)
            (output / (viewport + ".png")).write_bytes(raw)
            width, height = SIZES[viewport]
            images.append(
                {
                    "viewport": viewport,
                    "file": viewport + ".png",
                    "width": width,
                    "height": height,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "ink_ratio": 0.5,
                    "contrast": 255,
                }
            )
        return {"state": "rendered", "images": images, "runtime_image": self.image}


def completion(content, model):
    return httpx.Response(
        200,
        json={
            "id": "test-id",
            "model": model,
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        },
    )


def report(decision="pass", checked=None):
    return {
        "decision": decision,
        "checked_viewports": checked or ["desktop", "mobile"],
        "issues": []
        if decision == "pass"
        else [
            {
                "severity": "major",
                "location": "legend",
                "message": "图例遮挡坐标轴",
                "suggestion": "移动图例并增加边距",
            }
        ],
    }


@pytest.fixture
def pipeline(tmp_path):
    jobs = VisualizationJobStore(tmp_path / "tasks.sqlite", workspace_root=tmp_path)
    snapshots = SnapshotStore(jobs, tmp_path / "artifacts", workspace_root=tmp_path)
    receipts = ToolResultStore(tmp_path / "receipts.sqlite", workspace_root=tmp_path)
    context = ExecutionContext("alice", "thread-1")
    payload = {
        "status": "ok",
        "data": {"points": [{"x": 1, "y": 2}, {"x": 2, "y": None}]},
        "query": {"start": 0, "every_nth": 2},
    }
    receipt = receipts.put(
        context.scope_key, "run_fixture", "fixture-call", "get_scat_track_points", payload
    )
    refs = [
        {
            "kind": "readonly_result",
            "source_id": receipt["result_id"],
            "run_id": "run_fixture",
            "payload_sha256": receipt["payload_sha256"],
        }
    ]
    app = VisualizationTasks(jobs, snapshots, receipts)
    job = asyncio.run(
        app.prepare(
            context,
            "request-1",
            intent="画本页轨迹，明确采样范围",
            references=refs,
            preferences_sha256=digest({}),
            preferences={},
        )
    )
    settings = VisualizationSettings(
        enabled=True,
        code_api_key="fake-code",
        review_api_key="fake-review",
        code_base_url="https://api.deepseek.com",
        review_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_revisions=1,
    )
    sandbox = FakeSandbox()
    launcher = RenderLauncher(jobs, snapshots, sandbox)
    calls = {"code": 0, "review": 0}
    review_reports = [report()]
    code_sources = ["export async function render() {}"]

    def code(request):
        calls["code"] += 1
        body = json.loads(request.content)
        return completion(
            code_sources[min(calls["code"] - 1, len(code_sources) - 1)], body["model"]
        )

    def review(request):
        calls["review"] += 1
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        images = [v for v in body["messages"][1]["content"] if v["type"] == "image_url"]
        assert len(images) == 2 and all(
            v["image_url"]["url"].startswith("data:image/png;base64,") for v in images
        )
        value = review_reports[min(calls["review"] - 1, len(review_reports) - 1)]
        return completion(value if isinstance(value, str) else json.dumps(value), body["model"])

    worker = VisualizationWorker(
        app,
        launcher,
        settings,
        code_transport=httpx.MockTransport(code),
        review_transport=httpx.MockTransport(review),
    )
    return app, context, job, worker, calls, review_reports, code_sources, sandbox


def state(pipeline):
    app, context, job, *_ = pipeline
    return app.jobs.get(context, job["task_id"])["state"]


def test_complete_pipeline_publishes_reviewed_images_and_history_is_readonly(pipeline):
    app, context, job, worker, calls, *_ = pipeline
    assert asyncio.run(worker.run_once())
    assert state(pipeline) == "succeeded" and calls == {"code": 1, "review": 1}
    with app.jobs.transaction() as db:
        artifact = db.execute("SELECT artifact_id FROM visualization_publications").fetchone()[0]
    manifest = worker.publisher.get(context, artifact)
    assert manifest["files"]["desktop.png"]
    assert worker.publisher.read_file(context, artifact, "desktop.png").startswith(b"\x89PNG")
    assert (
        worker.publisher.publish(
            context, job["task_id"], "visualization-worker", 1, manifest["render_id"]
        )["artifact_id"]
        == artifact
    )
    with pytest.raises(JobNotFound):
        worker.publisher.get(ExecutionContext("bob", "thread-1"), artifact)
    with pytest.raises(JobNotFound):
        worker.publisher.read_file(context, artifact, "../../.env")
    assert calls == {"code": 1, "review": 1}


def test_independent_review_drives_a_new_code_candidate(pipeline):
    _, _, _, worker, calls, reports, _, sandbox = pipeline
    reports[:] = [report("revise"), report()]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "succeeded"
    assert calls == {"code": 2, "review": 2} and sandbox.calls == 2


def test_syntax_failure_is_fixed_before_spending_an_image_review(pipeline):
    _, _, _, worker, calls, _, sources, _ = pipeline
    sources[:] = [
        "export async function render() { syntax_bad }",
        "export async function render() {}",
    ]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "succeeded" and calls == {"code": 2, "review": 1}


@pytest.mark.parametrize(
    "answer",
    [
        report("reject"),
        report("pass", ["desktop"]),
        "not json",
        report("pass") | {"issues": report("revise")["issues"]},
    ],
)
def test_invalid_or_rejected_review_never_publishes(pipeline, answer):
    app, _, _, worker, _, reports, *_ = pipeline
    reports[:] = [answer]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "failed"
    with app.jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_publications").fetchone()[0] == 0


def test_revision_limit_is_enforced(pipeline):
    _, _, _, worker, calls, reports, *_ = pipeline
    reports[:] = [report("revise")]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "failed" and calls == {"code": 2, "review": 2}


@pytest.mark.parametrize("change", ["image", "revoke", "cancel"])
def test_changes_between_review_and_publish_block_delivery(pipeline, change, monkeypatch):
    app, context, job, worker, *_ = pipeline
    publish = worker.publisher.publish

    def changed(context_arg, task_id, worker_id, generation, render_id):
        if change == "image":
            path = app.snapshots.root / "renders" / render_id / "output/desktop.png"
            path.write_bytes(path.read_bytes() + b"changed")
        elif change == "revoke":
            app.snapshots.revoke(context, job["request"].snapshot_id)
        else:
            app.jobs.cancel(context, task_id)
        return publish(context_arg, task_id, worker_id, generation, render_id)

    monkeypatch.setattr(worker.publisher, "publish", changed)
    asyncio.run(worker.run_once())
    assert state(pipeline) in {"failed", "cancelled"}
    with app.jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_publications").fetchone()[0] == 0


def test_candidate_activation_and_phase_checkpoint_are_atomic(pipeline):
    app, _, job, worker, *_ = pipeline
    claimed = app.jobs.claim("manual")

    async def generate_only():
        result = await worker.code.complete(
            job["task_id"],
            "manual",
            claimed["generation"],
            call_key="atomic",
            messages=[],
            reserved_tokens=100,
            max_tokens=10,
        )
        with app.jobs.transaction() as db:
            call = db.execute("SELECT call_id FROM visualization_provider_calls").fetchone()[0]
        return worker.ledger.save_attempt(
            job["task_id"],
            "manual",
            claimed["generation"],
            step_key="atomic",
            code_call_id=call,
            source=result.content,
            activate=True,
        )

    asyncio.run(generate_only())
    assert (
        app.jobs.get(ExecutionContext("alice", "thread-1"), job["task_id"])["phase"] == "rendering"
    )


def test_launcher_service_auth_and_fixed_request_contract(pipeline):
    import threading

    from visualization_runtime.sandbox_service import server

    service = server(pipeline[3].launcher, "x" * 40, port=0)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        origin = "http://127.0.0.1:" + str(service.server_port)
        with httpx.Client(trust_env=False) as client:
            assert (
                client.post(origin + "/stopped", json={"resource_id": "anything"}).status_code
                == 403
            )
            response = client.post(
                origin + "/render",
                headers={"Authorization": "Bearer " + "x" * 40},
                json={
                    "task_id": "a" * 32,
                    "generation": 1,
                    "worker": "test",
                    "attempt_id": "b" * 32,
                    "source": "arbitrary code",
                    "path": "/etc",
                },
            )
            assert response.status_code == 400
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)


def test_known_schema_rejection_falls_back_to_strict_local_json_validation(pipeline):
    _, _, _, worker, *_ = pipeline
    requests = []

    def capability(request):
        body = json.loads(request.content)
        requests.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400)
        return completion(json.dumps(report()), body["model"])

    worker.reviewer.provider.transport = httpx.MockTransport(capability)
    asyncio.run(worker.run_once())
    assert state(pipeline) == "succeeded"
    assert requests == ["json_schema", "json_object"]


def test_revision_policy_is_checked_before_another_paid_generation(pipeline):
    _, _, _, worker, calls, reports, *_ = pipeline
    worker.settings.max_revisions = 0
    reports[:] = [report("revise")]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "failed" and calls == {"code": 1, "review": 1}


def test_single_schema_envelope_is_decoded_without_weakening_review(pipeline):
    _, _, _, worker, _, reports, *_ = pipeline
    reports[:] = [json.dumps([report()])]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "succeeded"


def test_multiple_schema_results_are_never_guessed_into_a_pass(pipeline):
    _, _, _, worker, _, reports, *_ = pipeline
    reports[:] = [json.dumps([report(), report("reject")])]
    asyncio.run(worker.run_once())
    assert state(pipeline) == "failed"


def test_full_summary_is_not_limited_to_preview_rows():
    from visualization_runtime.review import numeric_summary

    result = numeric_summary(
        {
            "datasets": [
                {"payload": {"points": [{"y": 800}, {"y": 1300}, {"y": None}, {"y": 22700}]}}
            ]
        }
    )
    assert result["dataset0.payload.points[].y"] == {
        "count": 3,
        "explicit_nulls": 1,
        "min": 800,
        "max": 22700,
    }


def test_temporal_summary_uses_full_timestamp_extent_without_assuming_timezone():
    from visualization_runtime.review import temporal_summary

    points = [{"timestamp": "2016-10-19T14:19:14.195312"}] * 8 + [
        {"timestamp": "2016-10-19T14:27:34.195312"}
    ]
    value = temporal_summary({"datasets": [{"payload": {"points": points}}]})[
        "dataset0.payload.points[].timestamp"
    ]
    assert value == {
        "count": 9,
        "timezone": "not_declared",
        "min": points[0]["timestamp"],
        "max": points[-1]["timestamp"],
        "span_seconds": 500.0,
    }


def test_temporal_summary_compares_offsets_and_refuses_mixed_naive_ranges():
    from visualization_runtime.review import temporal_summary

    def summarize(times):
        return temporal_summary({"datasets": [{"payload": {"times": times}}]})[
            "dataset0.payload.times[]"
        ]

    assert summarize(["2026-11-28T10:00:00+01:00", "2026-11-28T09:02:00Z"])["span_seconds"] == 120
    assert summarize(["2026-11-28T09:00:00", "2026-11-28T09:02:00Z"]) == {
        "count": 2,
        "timezone": "mixed",
    }
