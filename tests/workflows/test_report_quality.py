"""H6 lossless report reuse, pagination, numerical contracts and scoped persistence."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from application.context import ExecutionContext
from application.skill_worker import SkillWorker
from application.task_store import TaskConflict, TaskNotFound, digest
from schema.analysis import AnalysisResult
from tests.workflows.test_approved_workflows import app as app  # noqa: PLC0414 -- pytest fixture
from tests.workflows.test_approved_workflows import (
    approve,
    execute_flow,
    prepare,
    proposal,
)
from workflow_runtime.report import build


def legacy(dataset="scat", start="2020-01-01T00:00:00Z"):
    return AnalysisResult.model_validate(
        {
            "schema_version": "1.6",
            "dataset": dataset,
            "operation": "verified_old_result",
            "title": "旧七类多图结果",
            "start_time": start,
            "context": {"synthetic": True, "outcome": "partial", "missing_samples": 2},
            "blocks": [
                {
                    "type": "metrics",
                    "title": "指标",
                    "items": [
                        {"key": "ade", "label": "ADE", "value": 5.0, "unit": "m"},
                        {"key": "missing", "label": "缺失", "value": None},
                    ],
                },
                {
                    "type": "table",
                    "title": "原表",
                    "columns": [{"key": "zero", "label": "零"}],
                    "rows": [{"zero": 0}],
                },
                {
                    "type": "timeseries",
                    "title": "时序",
                    "x_key": "t",
                    "x_label": "时间",
                    "x_type": "number",
                    "x_unit": "s",
                    "series": [{"key": "v", "label": "速度", "unit": "m/s"}],
                    "points": [{"t": 0, "v": None}, {"t": 1, "v": 0}],
                },
                {
                    "type": "trajectory_2d",
                    "title": "快照",
                    "x_key": "x",
                    "x_label": "经度",
                    "y_key": "y",
                    "y_label": "纬度",
                    "snapshot_key": "snapshot",
                    "snapshot_label": "快照",
                    "points": [{"x": 110, "y": 30, "snapshot": "A"}],
                },
                {
                    "type": "classification",
                    "title": "分类",
                    "class_labels": ["正常"],
                    "items": [{"label": "正常", "count": 3, "confidence": 0.9}],
                },
                {
                    "type": "prediction",
                    "title": "预测",
                    "source": "fixed-model:1",
                    "target_label": "高度",
                    "target_unit": "m",
                    "points": [
                        {"time_step_s": 10, "value": 100, "lower_bound": 90, "upper_bound": 110}
                    ],
                },
                {
                    "type": "action_link",
                    "label": "三维",
                    "url": "/visualization/3d",
                    "kind": "visualization_3d",
                },
            ],
        }
    ).model_dump(mode="json")


def source(analysis, n=1):
    return {
        "artifact_id": f"old-result-{n}",
        "payload_sha256": digest(analysis),
        "analysis_result": analysis,
    }


def report(sources, bodies=(), kind="report"):
    pages = []
    analysis, outcome = build(
        proposal(kind=kind, external_ids=[], models=[], source_task_ids=["a" * 32])
        if kind == "report"
        else proposal(),
        {"synthetic": True, "sources": sources, "coverage": {"selected_count": 2}},
        list(bodies),
        [],
        pages=pages,
    )
    return analysis, outcome, pages


def test_seven_block_sources_keep_versions_units_origins_and_partial_status():
    first, second = legacy(), legacy("uav", "2023-01-01T00:00:00Z")
    result, outcome, pages = report([source(first), source(second, 2)])
    assert outcome == result["context"]["outcome"] == "partial"
    assert result["dataset"] == "cross_dataset"
    assert result["context"]["source_block_count"] == 14
    assert pages == [first, second]
    assert result["context"]["report_pages_sha256"] == digest(pages)
    assert result["context"]["report_page_count"] == 2
    assert first["start_time"] != second["start_time"]
    assert all(len(p["blocks"]) == 7 and p["schema_version"] == "1.6" for p in pages)


def test_more_than_twenty_blocks_are_not_dropped():
    sources = [source(legacy(), n) for n in range(8)]
    result, _, pages = report(sources)
    assert len(pages) == 8 and sum(len(p["blocks"]) for p in pages) == 56
    assert result["context"]["source_block_count"] == 56


def test_long_numeric_tables_are_paginated_without_rounding_or_missing_rows():
    metrics = [
        {"horizon_s": n + 1, "sample_count": 2, "mae_m": 1.23456789 + n, "rmse_m": 2.3456789 + n}
        for n in range(205)
    ]
    body = {
        "workflow_kind": "evaluation",
        "payload": {
            "status": "succeeded",
            "trial_count": 1,
            "common_sample_count": 2,
            "trials": [
                {
                    "trial_id": "fixed",
                    "configuration": {"model": "kalman-cv"},
                    "metrics": metrics,
                    "common_metrics": metrics,
                    "status": "succeeded",
                    "ungrouped_samples": 2,
                    "phase_metrics": [],
                    "worst_cases": [],
                }
            ],
        },
    }
    overview, outcome, pages = report([], [body], kind="comparison")
    assert outcome == "succeeded" and overview["context"]["metrics_total_rows"] == 205
    shown = next(b for b in overview["blocks"] if b["title"] == "模型与共同样本对比")
    assert len(shown["rows"]) == 100 and overview["context"]["report_overview"]
    rows = [
        r for p in pages for b in p["blocks"] if b["title"] == shown["title"] for r in b["rows"]
    ]
    assert len(rows) == 205
    assert [r["mae_m"] for r in rows] == [m["mae_m"] for m in metrics]
    assert all(r["common_count"] == 2 for r in rows)
    assert "not trajectory ADE/FDE" in overview["context"]["metric_definition"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("where", ["metric", "table", "context", "series", "prediction"])
def test_nonfinite_cannot_be_published_as_business_numbers(value, where):
    payload = legacy()
    if where == "metric":
        payload["blocks"][0]["items"][0]["value"] = value
    elif where == "table":
        payload["blocks"][1]["rows"][0]["zero"] = value
    elif where == "context":
        payload["context"]["bad"] = value
    elif where == "series":
        payload["blocks"][2]["points"][0]["v"] = value
    else:
        payload["blocks"][5]["points"][0]["value"] = value
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    ["//evil.test", "/\\evil.test", "https://user:pass@example.com", "https://", "/a\nLocation: x"],
)
def test_unsafe_saved_result_links_rejected(url):
    payload = legacy()
    payload["blocks"][-1]["url"] = url
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.asyncio
async def test_published_pages_are_durable_scoped_and_reused_without_models(app):
    context, original = await approve(app)
    await SkillWorker(app, execute_flow).once()
    original_run = app.runs.rows[original["run_id"]]
    primary = app.artifacts.rows[original_run.result_artifact_ids[0]]
    # Trusted repository fixture represents a completed legacy seven-block result.
    from analysis_runs.artifact_contracts import AnalysisArtifactRecord

    updated = AnalysisArtifactRecord.build(
        artifact_id=primary.artifact_id,
        run_id=primary.run_id,
        owner_user_id=primary.owner_user_id,
        thread_id=primary.thread_id,
        model_key=primary.model_key,
        model_version=primary.model_version,
        analysis_result=AnalysisResult.model_validate(legacy()),
        created_at=datetime.now(UTC),
    )
    app.artifacts.rows[primary.artifact_id] = updated
    _, prepared = await prepare(
        app,
        proposal(kind="report", external_ids=[], models=[], source_task_ids=[original["task_id"]]),
        context=context,
    )
    app.confirm(context, prepared["task_id"], prepared["snapshot_sha256"])
    await SkillWorker(app, execute_flow).once()
    view = await app.get(context, prepared["task_id"])
    assert view["skill_job"]["backend_executions"] == []
    ids = view["run"]["result_artifact_ids"]
    assert len(ids) == 2
    page = await app.artifact(context, prepared["task_id"], ids[1])
    assert page["analysis_result"] == legacy()
    # Separate application instance, same durable job database and repositories.
    from application.skill_job_factory import create_skill_tasks

    reopened = create_skill_tasks(
        app.skills.root,
        app.store.path,
        runs=app.runs,
        artifacts=app.artifacts,
        workflows_enabled=True,
        workflow_synthetic=True,
    )
    assert (await reopened.artifact(context, prepared["task_id"], ids[1])) == page
    with pytest.raises(TaskNotFound):
        await reopened.artifact(
            ExecutionContext(context.user_id, "other-thread"), prepared["task_id"], ids[1]
        )
    app.artifacts.rows[ids[1]] = app.artifacts.rows[ids[1]].model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    with pytest.raises(TaskNotFound):
        await app.artifact(context, prepared["task_id"], ids[1])
    app.artifacts.rows[ids[1]] = app.artifacts.rows[ids[1]].model_copy(
        update={"expires_at": None, "payload_sha256": "0" * 64}
    )
    with pytest.raises(TaskConflict):
        await app.artifact(context, prepared["task_id"], ids[1])
