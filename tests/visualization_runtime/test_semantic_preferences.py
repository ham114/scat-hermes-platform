"""Semantic proposals share budgets, provenance and the two-model preference snapshot."""

import asyncio
import json

import httpx
import pytest

from tests.visualization_runtime.test_delivery import parent, published
from tests.visualization_runtime.test_pipeline import completion
from tests.visualization_runtime.test_pipeline import pipeline as _pipeline
from tests.visualization_runtime.test_preferences import edit
from tests.visualization_runtime.test_preferences import memory as _memory
from visualization_runtime.contracts import CreateVisualization, ReviseVisualization
from visualization_runtime.preference_extraction import Decision

pipeline, memory = _pipeline, _memory
FEEDBACK = "把高度改为米，原始英尺数值必须乘0.3048，不能只改单位文字。保留全部点和数据范围，说明文字最多两行。后续高度单位未说明一律使用米"


def instrument(worker, decisions, *, during_extraction=None):
    code, review = worker.code.transport, worker.reviewer.provider.transport
    seen = {"extraction": [], "code": [], "review": []}

    def generate(request):
        body = json.loads(request.content)
        evidence = json.loads(body["messages"][1]["content"])
        if "CURRENT" in evidence:
            seen["extraction"].append(evidence)
            if during_extraction:
                during_extraction()
            return completion(
                json.dumps(decisions[evidence["CURRENT"]], ensure_ascii=False), body["model"]
            )
        seen["code"].append(evidence["preferences"])
        return code.handle_request(request)

    def inspect(request):
        body = json.loads(request.content)
        seen["review"].append(json.loads(body["messages"][1]["content"][0]["text"])["preferences"])
        return review.handle_request(request)

    worker.code.transport = httpx.MockTransport(generate)
    worker.reviewer.provider.transport = httpx.MockTransport(inspect)
    return seen


def test_feedback_remembered_for_next_creation_and_local_override_reaches_both_models(
    pipeline, memory
):
    app, context, old, worker, *_ = pipeline
    artifact = published(pipeline)
    app.preferences, app.semantic_preferences = memory.charts, True
    parent(app, context)
    state = memory.charts.list(context.user_id)
    decisions = {
        FEEDBACK: {
            "current": [],
            "durable": [
                {
                    "key": "altitude_unit",
                    "value": "m",
                    "task_type": "all",
                    "evidence": "后续高度单位未说明一律使用米",
                }
            ],
        },
        "改为104362": {"current": [], "durable": []},
        "仅这张图用英尺": {
            "current": [{"key": "altitude_unit", "value": "ft", "evidence": "仅这张图用英尺"}],
            "durable": [],
        },
    }
    observed = instrument(worker, decisions)
    revision = app.revise(
        context,
        artifact,
        ReviseVisualization(
            request_id="learn-m",
            feedback=FEEDBACK,
            memory_epoch=state["epoch"],
            memory_revision=state["revision"],
        ),
        parent_run_id="run_fixture",
        learn=True,
    )
    assert memory.charts.list(context.user_id)["items"] == []  # no regex write before semantics
    assert asyncio.run(worker.run_once())
    assert app.view(context, revision["task_id"]).memory_outcome == "saved"
    record = memory.charts.list(context.user_id)["items"][0]
    assert (record["key"], record["value"], record["source_artifact"], record["evidence"]) == (
        "altitude_unit",
        "m",
        artifact,
        FEEDBACK,
    )
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    for index, intent in enumerate(["改为104362", "仅这张图用英尺"]):
        job = asyncio.run(
            app.create(
                context,
                CreateVisualization(
                    request_id="next-" + str(index), intent=intent, data_refs=[refs]
                ),
                parent_run_id="run_fixture",
            )
        )
        assert asyncio.run(worker.run_once())
        assert app.view(context, job["task_id"]).state == "succeeded"
    expected = [{"altitude_unit": "m"}, {"altitude_unit": "m"}, {"altitude_unit": "ft"}]
    assert observed["code"] == observed["review"] == expected
    assert memory.charts.list(context.user_id)["items"][0]["value"] == "m"
    with app.jobs.transaction() as db:
        assert (
            db.execute(
                "SELECT learned FROM visualization_preference_bindings WHERE task_id=?",
                (revision["task_id"],),
            ).fetchone()[0]
            == 1
        )
    assert app.view(context, old["task_id"]).artifact_id == artifact
    with app.jobs.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM visualization_provider_calls WHERE call_key='preference-extraction'"
            ).fetchone()[0]
            == 3
        )


@pytest.mark.parametrize(
    "action,expected", [("pause", "disabled"), ("edit", "conflict"), ("forget", "conflict")]
)
def test_memory_changed_during_extraction_never_resurrects_old_default(
    pipeline, memory, action, expected
):
    app, context, old, worker, *_ = pipeline
    app.jobs.cancel(context, old["task_id"])
    app.preferences, app.semantic_preferences = memory.charts, True
    parent(app, context)
    state = memory.charts.list(context.user_id)
    text = "所有高度图缺省按米显示"
    decisions = {
        text: {
            "current": [],
            "durable": [
                {"key": "altitude_unit", "value": "m", "task_type": "all", "evidence": text}
            ],
        }
    }

    def change():
        if action == "pause":
            memory.settings(context.user_id, False)
        elif action == "forget":
            memory.forget(context.user_id)
            memory.settings(context.user_id, True)
        else:
            edit(memory, context.user_id, "altitude_unit", "ft")

    seen = instrument(worker, decisions, during_extraction=change)
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    job = asyncio.run(
        app.create(
            context,
            CreateVisualization(
                request_id="race",
                intent=text,
                data_refs=[refs],
                memory_epoch=state["epoch"],
                memory_revision=state["revision"],
            ),
            parent_run_id="run_fixture",
            learn=True,
        )
    )
    assert asyncio.run(worker.run_once())
    view = app.view(context, job["task_id"])
    assert view.state == "succeeded" and view.memory_outcome == expected
    assert (
        len(seen["extraction"]) == 1
    )  # a saved classifier reply is not resent after memory refresh
    assert seen["code"] == seen["review"] == [{"altitude_unit": "m"}]
    records = memory.charts.list(context.user_id)["items"]
    assert not any(r["value"] == "m" for r in records)


def test_new_visual_rules_and_withdrawal_do_not_need_new_extractors(pipeline, memory):
    app, context, old, worker, *_ = pipeline
    app.jobs.cancel(context, old["task_id"])
    app.preferences, app.semantic_preferences = memory.charts, True
    parent(app, context)
    edit(memory, context.user_id, "legend_position", "right")
    text = "别再默认放右侧图例了，恢复自动。后面的图遇到长标签请倾斜45度。"
    state = memory.charts.list(context.user_id)
    decisions = {
        text: {
            "current": [],
            "durable": [
                {
                    "key": "legend_position",
                    "value": None,
                    "task_type": "all",
                    "evidence": "别再默认放右侧图例了，恢复自动",
                },
                {
                    "key": "rule_long_label_rotation",
                    "value": "长标签倾斜45度",
                    "task_type": "all",
                    "evidence": "后面的图遇到长标签请倾斜45度",
                },
            ],
        }
    }
    seen = instrument(worker, decisions)
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    job = asyncio.run(
        app.create(
            context,
            CreateVisualization(
                request_id="new-rule",
                intent=text,
                data_refs=[refs],
                memory_epoch=state["epoch"],
                memory_revision=state["revision"],
            ),
            parent_run_id="run_fixture",
            learn=True,
        )
    )
    assert asyncio.run(worker.run_once())
    assert app.view(context, job["task_id"]).memory_outcome == "saved"
    assert seen["code"] == seen["review"] == [{"rule_long_label_rotation": "长标签倾斜45度"}]
    assert [r["key"] for r in memory.charts.list(context.user_id)["items"]] == [
        "rule_long_label_rotation"
    ]


def test_invented_evidence_fails_without_memory_or_chart_delivery(pipeline, memory):
    app, context, old, worker, *_ = pipeline
    app.jobs.cancel(context, old["task_id"])
    app.preferences, app.semantic_preferences = memory.charts, True
    parent(app, context)
    seen = instrument(
        worker,
        {
            "画图": {
                "current": [],
                "durable": [
                    {"key": "palette", "value": "red", "task_type": "all", "evidence": "以后用红色"}
                ],
            }
        },
    )
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    job = asyncio.run(
        app.create(
            context,
            CreateVisualization(request_id="bad-evidence", intent="画图", data_refs=[refs]),
            parent_run_id="run_fixture",
        )
    )
    assert asyncio.run(worker.run_once())
    view = app.view(context, job["task_id"])
    assert (
        view.state == "failed" and view.error_code == "invalid_visualization_preference_extraction"
    )
    assert seen["code"] == seen["review"] == [] and not memory.charts.list(context.user_id)["items"]


def test_model_proposals_cannot_bypass_value_and_key_validation():
    for change in [
        {"key": "altitude_unit", "value": 999, "evidence": "999"},
        {"key": "disable_review", "value": "true", "evidence": "true"},
        {"key": "rule_x", "value": "sk-abcdefghijklmnopqrst", "evidence": "secret"},
    ]:
        with pytest.raises(ValueError):
            Decision.model_validate({"current": [change], "durable": []})


def test_saved_extraction_response_is_reused_after_interruption(pipeline, memory, monkeypatch):
    from visualization_runtime import preference_extraction as extraction

    app, context, old, worker, *_ = pipeline
    app.jobs.cancel(context, old["task_id"])
    app.preferences, app.semantic_preferences = memory.charts, True
    parent(app, context)
    text = "今后的高度图统一用米"
    state = memory.charts.list(context.user_id)
    seen = instrument(
        worker,
        {
            text: {
                "current": [],
                "durable": [
                    {"key": "altitude_unit", "value": "m", "evidence": text, "task_type": "all"},
                ],
            }
        },
    )
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    job = asyncio.run(
        app.create(
            context,
            CreateVisualization(
                request_id="restart",
                intent=text,
                data_refs=[refs],
                memory_epoch=state["epoch"],
                memory_revision=state["revision"],
            ),
            parent_run_id="run_fixture",
            learn=True,
        )
    )
    claimed = app.jobs.claim("restart-worker")
    original = extraction.apply

    def interrupted(*args):
        raise RuntimeError("fixture_interruption_before_apply")

    monkeypatch.setattr(extraction, "apply", interrupted)
    with pytest.raises(RuntimeError, match="fixture_interruption"):
        asyncio.run(
            extraction.resolve(
                worker, context, job["task_id"], "restart-worker", claimed["generation"]
            )
        )
    assert memory.charts.list(context.user_id)["items"] == []
    monkeypatch.setattr(extraction, "apply", original)
    asyncio.run(
        extraction.resolve(worker, context, job["task_id"], "restart-worker", claimed["generation"])
    )
    assert len(seen["extraction"]) == 1
    assert app.view(context, job["task_id"]).memory_outcome == "saved"
    assert memory.charts.list(context.user_id)["items"][0]["value"] == "m"


def test_capacity_failure_rolls_back_memory_write_and_task_preferences(pipeline, memory):
    app, context, old, worker, *_ = pipeline
    app.jobs.cancel(context, old["task_id"])
    app.preferences, app.semantic_preferences = memory.charts, True
    parent(app, context)
    for index in range(31):
        edit(memory, context.user_id, "rule_existing_" + str(index), "保留原规则")
    before = memory.charts.list(context.user_id)
    text = "这张标签斜一点并多些留白，长期用蓝色"
    instrument(
        worker,
        {
            text: {
                "current": [
                    {"key": "rule_rotation", "value": "倾斜标签", "evidence": "标签斜一点"},
                    {"key": "rule_spacing", "value": "增加留白", "evidence": "多些留白"},
                ],
                "durable": [
                    {
                        "key": "palette",
                        "value": "blue",
                        "evidence": "长期用蓝色",
                        "task_type": "all",
                    }
                ],
            }
        },
    )
    refs = app.require_snapshot(context, old["task_id"])["payload"]["datasets"][0]["reference"]
    job = asyncio.run(
        app.create(
            context,
            CreateVisualization(
                request_id="capacity",
                intent=text,
                data_refs=[refs],
                memory_epoch=before["epoch"],
                memory_revision=before["revision"],
            ),
            parent_run_id="run_fixture",
            learn=True,
        )
    )
    assert asyncio.run(worker.run_once())
    view = app.view(context, job["task_id"])
    assert (
        view.state == "failed" and view.error_code == "visualization_preference_capacity_exceeded"
    )
    assert memory.charts.list(context.user_id) == before
