"""Dialogue continuity is independent from cross-session preference storage."""

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from application.context import ExecutionContext
from application.visualization_tasks import VisualizationTasks
from hermes_runtime.registry import RunRegistry
from scripts.run_visualization_tool import trusted_delegation
from tests.visualization_runtime.test_delivery import parent
from tests.visualization_runtime.test_pipeline import completion
from tests.visualization_runtime.test_pipeline import pipeline as _pipeline
from tests.visualization_runtime.test_preferences import memory as _memory
from visualization_runtime.contracts import CreateVisualization, ReviseVisualization
from visualization_runtime.store import JobConflict

pipeline, memory = _pipeline, _memory


def configure(pipeline, memory=None):
    app, context, old, worker, *_ = pipeline
    app.jobs.cancel(context, old["task_id"])
    app.delegation_enabled = True
    if memory:
        app.preferences = memory.charts
    parent(app, context)
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    observed = {"code": [], "review": []}
    updates = []
    original = worker.reviewer.provider.transport

    def generate(request):
        body = json.loads(request.content)
        assert body.get("response_format") == {"type": "json_object"}
        evidence = json.loads(body["messages"][1]["content"])
        assert "CURRENT" not in evidence  # no separate preference extraction
        observed["code"].append(evidence)
        return completion(
            json.dumps({"source": "export async function render() {}", "memory_changes": updates}),
            body["model"],
        )

    def inspect(request):
        body = json.loads(request.content)
        observed["review"].append(json.loads(body["messages"][1]["content"][0]["text"]))
        return original.handle_request(request)

    worker.code.transport = httpx.MockTransport(generate)
    worker.reviewer.provider.transport = httpx.MockTransport(inspect)
    return app, context, worker, refs, observed, updates


def create(app, context, refs, intent, **options):
    state = app.preferences.list(context.user_id) if app.preferences else None
    return asyncio.run(
        app.create(
            context,
            CreateVisualization(
                request_id=options.pop("request_id", uuid4().hex),
                intent=intent,
                data_refs=[refs],
                memory_epoch=state["epoch"] if state else None,
                memory_revision=state["revision"] if state else None,
            ),
            parent_run_id="run_fixture",
            **options,
        )
    )


def finish(app, context, worker, job):
    assert asyncio.run(worker.run_once())
    view = app.view(context, job["task_id"])
    assert view.state == "succeeded", view.error_code
    return view.artifact_id


def test_no_long_term_memory_required_for_card_to_next_chart_continuity(pipeline):
    app, context, worker, refs, observed, _ = configure(pipeline)
    first = create(app, context, refs, "绘制104363的高度时间折线图，注明本页范围，原始英尺")
    artifact = finish(app, context, worker, first)
    feedback = "把高度换算成米，保留原始点和本页范围，不作为长期偏好"
    second = app.revise(
        context,
        artifact,
        ReviseVisualization(request_id="card-m", feedback=feedback),
        parent_run_id="run_fixture",
    )
    finish(app, context, worker, second)
    receipt = app.receipts.put(
        context.scope_key,
        "run_fixture",
        "different-flight",
        "get_scat_track_points",
        {"status": "ok", "data": {"points": [{"altitude_ft": 2000}]}},
    )
    new_ref = {
        "kind": "readonly_result",
        "source_id": receipt["result_id"],
        "run_id": "run_fixture",
        "payload_sha256": receipt["payload_sha256"],
    }
    third = create(app, context, new_ref, "改为104362")
    finish(app, context, worker, third)
    packet = third["request"].delegation
    assert packet.user_message == "改为104362"
    assert feedback in [message.content for message in packet.history]
    assert first["request"].snapshot_sha256 != third["request"].snapshot_sha256
    assert third["request"].parent_artifact_id is None
    for code, review in zip(observed["code"], observed["review"], strict=True):
        assert code["delegation_context"] == review["delegation_context"]
        assert code["context_sha256"] == review["context_sha256"]
    with app.jobs.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM visualization_provider_calls WHERE call_key='preference-extraction'"
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM visualization_provider_calls").fetchone()[0] == 6
    assert (
        app.view(context, third["task_id"]).context_sha256 == observed["code"][-1]["context_sha256"]
    )


def test_native_brief_and_current_question_are_preserved_separately(pipeline, tmp_path):
    app, context, worker, refs, _, _ = configure(pipeline)
    registry = RunRegistry(tmp_path / "bindings.sqlite", workspace_root=tmp_path)
    with registry._connect() as db:
        db.execute(
            "INSERT INTO runtime_threads VALUES(?,?,0)", (context.thread_id, context.user_id)
        )
    for index, text in enumerate(["高度图改成米", "改为104362"]):
        request = uuid4().hex
        registry.reserve_request(context, request, hashlib.sha256(text.encode()).hexdigest(), text)
        registry.bind(context, request, "run_" + str(index))
        registry.finish_run(context, "run_" + str(index))
    raw = trusted_delegation(registry, context, "run_1")
    brief = "绘制104362高度随时间折线图，数值换算为米，保留采样范围；不要增加速度面板"
    job = create(app, context, refs, brief, **raw)
    finish(app, context, worker, job)
    packet = job["request"].delegation
    assert job["request"].intent == packet.task_brief == brief
    assert packet.user_message == "改为104362"
    assert [item.content for item in packet.history] == ["高度图改成米"]
    with pytest.raises(LookupError):
        trusted_delegation(registry, ExecutionContext("other", context.thread_id), "run_1")
    with registry._connect() as db:
        db.execute("UPDATE conversation_turns SET input='伪造的历史要求' WHERE run_id='run_0'")
    with pytest.raises(ValueError, match="trusted_user_message"):
        registry.delegation_history(context, "run_1")


def test_older_target_is_kept_when_another_chart_was_created_later(pipeline):
    app, context, worker, refs, observed, _ = configure(pipeline)
    first = create(app, context, refs, "第一张高度图，蓝色主线")
    first_artifact = finish(app, context, worker, first)
    second = create(app, context, refs, "另起任务：第二张速度图，红色主线")
    second_artifact = finish(app, context, worker, second)
    revised = app.revise(
        context,
        first_artifact,
        ReviseVisualization(
            request_id="older", feedback="把第一张的标题改成高度复核，保留原来的内容"
        ),
        parent_run_id="run_fixture",
    )
    finish(app, context, worker, revised)
    packet = observed["code"][-1]["delegation_context"]
    assert packet["target_artifact_id"] == first_artifact
    assert {c["artifact_id"] for c in packet["related_charts"]} == {first_artifact, second_artifact}
    assert revised["request"].snapshot_id == first["request"].snapshot_id


def test_context_survives_restart_and_idempotent_retry_without_mutating_old_task(pipeline):
    app, context, worker, refs, _, _ = configure(pipeline)
    first = create(
        app, context, refs, "高度默认按当前分析的米制画，不保存长期记忆", request_id="first"
    )
    finish(app, context, worker, first)
    frozen = app.view(context, first["task_id"]).context_sha256
    restarted = VisualizationTasks(
        app.jobs, app.snapshots, app.receipts, parent_budget_path=app.parent_budget_path
    )
    second = create(restarted, context, refs, "换一组数据继续画")
    assert second["request"].delegation.history[0].content == first["request"].intent
    same = create(
        restarted, context, refs, "高度默认按当前分析的米制画，不保存长期记忆", request_id="first"
    )
    assert same["task_id"] == first["task_id"]
    assert restarted.view(context, first["task_id"]).context_sha256 == frozen
    assert second["request"].delegation.revision == 2
    with pytest.raises(JobConflict, match="request_id_conflict"):
        create(
            restarted,
            context,
            refs,
            first["request"].intent,
            request_id="first",
            user_message="不同用户要求不能借旧请求号复用",
        )


def test_oversized_context_rolls_back_dialogue_and_parent_reservation(pipeline):
    app, context, _, refs, _, _ = configure(pipeline)
    with app.jobs.transaction() as db:
        parent_path = app.parent_budget_path
    import sqlite3

    with sqlite3.connect(parent_path) as db:
        before = db.execute("SELECT charged_tokens FROM skill_run_budgets").fetchone()[0]
    history = [
        {
            "source_id": "history-" + str(index),
            "role": "user",
            "content": "重要要求" * 1000,
            "at": index,
        }
        for index in range(3)
    ]
    with pytest.raises(JobConflict, match="context_too_large"):
        create(app, context, refs, "继续", conversation=history)
    with app.jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_dialogue").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM visualization_dialogue_heads").fetchone()[0] == 0
    with sqlite3.connect(parent_path) as db:
        assert db.execute("SELECT charged_tokens FROM skill_run_budgets").fetchone()[0] == before


def test_memory_event_is_published_atomically_without_extra_model_call(pipeline, memory):
    app, context, worker, refs, _, changes = configure(pipeline, memory)
    text = "以后高度默认用米，请画本页高度图"
    changes.append({"key": "altitude_unit", "value": "m", "evidence": "以后高度默认用米"})
    job = create(app, context, refs, text, learn=True)
    assert not memory.charts.list(context.user_id)["items"]
    finish(app, context, worker, job)
    assert app.view(context, job["task_id"]).memory_outcome == "saved"
    records = memory.charts.list(context.user_id)["items"]
    assert len(records) == 1 and records[0]["value"] == "m" and records[0]["evidence"] == text
    assert not memory.charts.list("other")["items"]
    with app.jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_provider_calls").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM visualization_preference_inputs").fetchone()[0] == 0


def test_main_agent_summary_cannot_authorize_a_new_memory_write(pipeline, memory):
    app, context, worker, refs, _, changes = configure(pipeline, memory)
    changes.append({"key": "palette", "value": "red", "evidence": "以后默认红色"})
    job = create(app, context, refs, "以后默认红色，画高度图", user_message="画高度图", learn=True)
    assert asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).error_code == "invalid_delegated_generation"
    assert not memory.charts.list(context.user_id)["items"]
    with app.jobs.transaction() as db:
        assert not db.execute("SELECT 1 FROM visualization_publications").fetchone()


@pytest.mark.parametrize("action,expected", [("pause", "disabled"), ("forget", "conflict")])
def test_memory_withdrawal_during_generation_cannot_be_restored_at_publication(
    pipeline, memory, action, expected
):
    app, context, worker, refs, _, changes = configure(pipeline, memory)
    changes.append({"key": "altitude_unit", "value": "m", "evidence": "以后用米"})
    original = worker.code.transport
    changed = False

    def send(request):
        nonlocal changed
        if not changed:
            changed = True
            if action == "pause":
                memory.settings(context.user_id, False)
            else:
                memory.forget(context.user_id)
                memory.settings(context.user_id, True)
        return original.handle_request(request)

    worker.code.transport = httpx.MockTransport(send)
    job = create(app, context, refs, "以后用米，画高度", learn=True)
    finish(app, context, worker, job)
    assert app.view(context, job["task_id"]).memory_outcome == expected
    assert not memory.charts.list(context.user_id)["items"]


def test_public_tool_contracts_add_no_scope_or_chart_template_parameters():
    assert set(CreateVisualization.model_fields) == {
        "request_id",
        "intent",
        "data_refs",
        "memory_epoch",
        "memory_revision",
    }
    assert set(ReviseVisualization.model_fields) == {
        "request_id",
        "feedback",
        "memory_epoch",
        "memory_revision",
    }


def test_tampered_context_is_rejected_before_a_new_task(pipeline):
    app, context, worker, refs, _, _ = configure(pipeline)
    first = create(app, context, refs, "本轮高度用米")
    finish(app, context, worker, first)
    with app.jobs.transaction() as db:
        db.execute("UPDATE visualization_dialogue SET content='篡改为英尺'")
    with pytest.raises(JobConflict, match="dialogue_integrity"):
        create(app, context, refs, "继续")


def test_bad_review_does_not_save_pending_memory(pipeline, memory):
    app, context, worker, refs, _, changes = configure(pipeline, memory)
    changes.append({"key": "palette", "value": "blue", "evidence": "以后用蓝色"})
    pipeline[5][:] = [
        {
            "decision": "reject",
            "checked_viewports": ["desktop", "mobile"],
            "issues": [
                {
                    "severity": "blocker",
                    "location": "data",
                    "message": "缺少必要数据",
                    "suggestion": "补齐数据",
                }
            ],
        }
    ]
    job = create(app, context, refs, "以后用蓝色，画本页数据", learn=True)
    assert asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).state == "failed"
    assert not memory.charts.list(context.user_id)["items"]


def test_publication_failure_rolls_back_its_memory_event(pipeline, memory):
    app, context, worker, refs, _, changes = configure(pipeline, memory)
    changes.append({"key": "palette", "value": "blue", "evidence": "以后用蓝色"})
    with app.jobs.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_publication BEFORE INSERT ON visualization_publications BEGIN SELECT RAISE(ABORT,'fixture_failed'); END"
        )
    job = create(app, context, refs, "以后用蓝色，画本页数据", learn=True)
    assert asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).state == "failed"
    assert not memory.charts.list(context.user_id)["items"]
    with app.jobs.transaction() as db:
        assert not db.execute("SELECT 1 FROM visualization_delegation_memory").fetchone()


def test_assistant_verbosity_does_not_drop_user_requirements(pipeline):
    app, context, _, refs, _, _ = configure(pipeline)
    history = [
        {"source_id": "m", "role": "user", "content": "高度用米", "at": 0},
        {
            "source_id": "answer",
            "role": "assistant",
            "content": "这是很长的辅助说明" * 700,
            "at": 1,
        },
    ]
    job = create(app, context, refs, "换数据", conversation=history)
    packet = job["request"].delegation
    assert packet.history[0].content == "高度用米"
    assert "未展开" in packet.history[1].content


def test_known_provider_format_tag_does_not_relax_delegation_validation(pipeline):
    from visualization_runtime.delegation import parse_generation

    app, context, _, refs, _, _ = configure(pipeline)
    job = create(app, context, refs, "画图")
    body = {"source": "export async function render() {}", "memory_changes": []}
    assert (
        parse_generation(json.dumps(body | {"type": "json_object"}), job["request"]).source
        == body["source"]
    )
    for extra in ({"type": "skip_validation"}, {"scope": "all"}, {"untrusted": True}):
        with pytest.raises(JobConflict, match="invalid_delegated_generation"):
            parse_generation(json.dumps(body | extra), job["request"])


def test_missing_memory_receipt_does_not_block_chart_or_claim_saved(pipeline, memory):
    app, context, worker, refs, _, _ = configure(pipeline, memory)
    worker.code.transport = httpx.MockTransport(
        lambda request: completion(
            json.dumps({"source": "export async function render() {}"}),
            json.loads(request.content)["model"],
        )
    )
    job = create(app, context, refs, "以后用蓝色，画本页图", learn=True)
    finish(app, context, worker, job)
    assert app.view(context, job["task_id"]).memory_outcome == "unconfirmed"
    assert not memory.charts.list(context.user_id)["items"]


def test_natural_language_condition_is_preserved_and_reviewed_without_scope_fields(
    pipeline, memory
):
    app, context, worker, refs, observed, changes = configure(pipeline, memory)
    text = "以后高度未说明单位就默认以米显示。"
    condition = "未说明单位时默认以米(m)显示"
    changes.append({"key": "altitude_unit", "value": condition, "evidence": text})
    job = create(app, context, refs, text, learn=True)
    finish(app, context, worker, job)
    assert memory.charts.list(context.user_id)["items"][0]["value"] == condition
    assert observed["review"][0]["proposed_memory_changes"] == changes
    assert "scope" not in observed["review"][0]["proposed_memory_changes"][0]
