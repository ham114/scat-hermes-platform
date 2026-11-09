"""Deterministic report construction from verified numerical artifacts."""

from __future__ import annotations

from application.task_store import digest
from schema.analysis import AnalysisResult, MetricItem, MetricsBlock, TableBlock, TableColumn


def table(title, columns, rows):
    return TableBlock(
        title=title,
        columns=[TableColumn(key=key, label=label, unit=unit) for key, label, unit in columns],
        rows=rows[:100],
    )


def build(proposal, frozen, bodies, references, *, pages=None):
    complete_tables = []

    def report_table(title, columns, rows):
        for offset in range(0, max(1, len(rows)), 100):
            complete_tables.append(table(title, columns, rows[offset : offset + 100]))
        return table(title, columns, rows)

    coverage = frozen.get("coverage", {})
    summaries = [b["payload"] for b in bodies if b["workflow_kind"] == "evaluation"]
    materializations = [b["payload"] for b in bodies if b["workflow_kind"] == "materialization"]
    status = "succeeded"
    if any(
        s["analysis_result"]["context"].get("outcome") in {"partial", "failed"}
        for s in frozen.get("sources", [])
    ):
        status = "partial"
    if any(s.get("status") != "succeeded" for s in summaries + materializations):
        status = "partial"
    if (
        not summaries
        and materializations
        and all(m.get("status") == "failed" for m in materializations)
    ):
        status = "failed"
    if (
        summaries
        and not any(s.get("prediction_count") for s in summaries)
        and all(
            not any(m["sample_count"] for t in s["trials"] for m in t["metrics"]) for s in summaries
        )
    ):
        status = "failed"
    blocks = [
        MetricsBlock(
            title="执行覆盖与口径",
            items=[
                MetricItem(
                    key="selected_flights",
                    label="固定样本航班",
                    value=coverage.get("selected_count", 0),
                ),
                MetricItem(
                    key="trials", label="批准试验数", value=sum(s["trial_count"] for s in summaries)
                ),
                MetricItem(
                    key="common_samples",
                    label="共同可比样本",
                    value=sum(s["common_sample_count"] for s in summaries),
                ),
                MetricItem(key="outcome", label="结果状态", value=status),
            ],
        )
    ]
    metric_rows, phase_rows, worst_rows = [], [], []
    for summary in summaries:
        for trial in summary["trials"]:
            common = {m["horizon_s"]: m for m in trial["common_metrics"]}
            for m in trial["metrics"]:
                shared = common.get(m["horizon_s"], {"sample_count": 0, "mae_m": None})
                metric_rows.append(
                    {
                        "trial_id": trial["trial_id"],
                        "model": trial["configuration"]["model"],
                        "horizon_s": m["horizon_s"],
                        "sample_count": m["sample_count"],
                        "mae_m": m["mae_m"],
                        "rmse_m": m["rmse_m"],
                        "common_count": shared["sample_count"],
                        "common_mae_m": shared["mae_m"],
                        "status": trial["status"],
                        "ungrouped": trial["ungrouped_samples"],
                    }
                )
            phase_rows.extend({"trial_id": trial["trial_id"], **r} for r in trial["phase_metrics"])
            worst_rows.extend({"trial_id": trial["trial_id"], **r} for r in trial["worst_cases"])
    if proposal.kind == "forecast" and metric_rows:
        raise ValueError("Future forecasts cannot produce error metric tables")
    windows = [w for f in frozen.get("flights", []) for w in f.get("observation_windows", [])]
    if windows and (proposal.kind == "forecast" or proposal.observation_last_n or proposal.as_of):
        blocks.append(
            report_table(
                "已冻结观测窗口与预测起点",
                [
                    ("external_id", "航班", None),
                    ("point_count", "观测点数", None),
                    ("start_time", "观测起点 UTC", None),
                    ("forecast_origin", "预测起点 UTC", None),
                    ("span_s", "实际观测跨度", "s"),
                    ("mode", "预测/回测", None),
                ],
                [
                    {
                        k: w[k]
                        for k in (
                            "external_id",
                            "point_count",
                            "start_time",
                            "forecast_origin",
                            "span_s",
                            "mode",
                        )
                    }
                    for w in windows
                ],
            )
        )
    if metric_rows:
        blocks.append(
            report_table(
                "模型与共同样本对比",
                [
                    ("trial_id", "试验", None),
                    ("model", "模型", None),
                    ("horizon_s", "时域", "s"),
                    ("sample_count", "本模型样本", None),
                    ("mae_m", "MAE", "m"),
                    ("rmse_m", "RMSE", "m"),
                    ("common_count", "共同样本", None),
                    ("common_mae_m", "共同 MAE", "m"),
                    ("status", "状态", None),
                    ("ungrouped", "未归组样本", None),
                ],
                metric_rows,
            )
        )
    if phase_rows:
        blocks.append(
            report_table(
                "阶段分组",
                [
                    ("trial_id", "试验", None),
                    ("phase", "阶段", None),
                    ("horizon_s", "时域", "s"),
                    ("sample_count", "样本数", None),
                    ("flight_count", "航班数", None),
                    ("mae_m", "MAE", "m"),
                    ("median_m", "中位数", "m"),
                    ("p90_m", "P90", "m"),
                ],
                phase_rows,
            )
        )
    if worst_rows:
        blocks.append(
            report_table(
                "最差案例",
                [
                    ("trial_id", "试验", None),
                    ("external_id", "航班", None),
                    ("cutoff_time", "观测截止时刻", None),
                    ("horizon_s", "时域", "s"),
                    ("error_m", "误差", "m"),
                ],
                worst_rows,
            )
        )
    if materializations:
        blocks.append(
            report_table(
                "派生数据构建",
                [("kind", "派生类型", None), ("records", "记录数", None), ("status", "状态", None)],
                [
                    {"kind": m["kind"], "records": m["record_count"], "status": m["status"]}
                    for m in materializations
                ],
            )
        )
    originals = [
        AnalysisResult.model_validate(source["analysis_result"])
        for source in frozen.get("sources", [])
    ]
    if originals:
        blocks.append(
            report_table(
                "分项结果索引（完整内容见独立制品）",
                [
                    ("artifact_id", "原制品", None),
                    ("dataset", "数据集", None),
                    ("start_time", "UTC起点", None),
                    ("blocks", "内容块数", None),
                ],
                [
                    {
                        "artifact_id": source["artifact_id"],
                        "dataset": original.dataset,
                        "start_time": original.start_time,
                        "blocks": len(original.blocks),
                    }
                    for source, original in zip(frozen["sources"], originals, strict=True)
                ],
            )
        )
    provenance = [
        {"artifact_id": ref["artifact_id"], "content_sha256": ref["content_sha256"]}
        for ref in references
    ]
    provenance.extend(
        {"artifact_id": source["artifact_id"], "content_sha256": source["payload_sha256"]}
        for source in frozen.get("sources", [])
    )
    blocks.append(
        report_table(
            "结果来源",
            [("artifact_id", "制品", None), ("content_sha256", "摘要", None)],
            provenance,
        )
    )
    analysis = AnalysisResult(
        schema_version="1.4",
        dataset=(
            originals[0].dataset
            if originals and len({p.dataset for p in originals}) == 1
            else "cross_dataset"
            if originals
            else "scat"
        ),
        operation="skill_workflow_" + proposal.kind,
        title=proposal.title,
        context={
            "workflow_kind": proposal.kind,
            "protocol": "point-exact-prediction/v1"
            if proposal.kind == "forecast" or proposal.observation_last_n or proposal.as_of
            else "m6-eval-v1",
            "prediction_mode": "future_forecast"
            if proposal.kind == "forecast"
            else "historical_backtest"
            if proposal.models
            else "not_applicable",
            "prediction_count": sum(s.get("prediction_count", 0) for s in summaries),
            "metric_availability": "available" if metric_rows else "no_verified_truth",
            "outcome": status,
            "synthetic": frozen["synthetic"],
            "metrics_total_rows": len(metric_rows),
            "phase_total_rows": len(phase_rows),
            "worst_total_rows": len(worst_rows),
            "table_row_limit": 100,
            "source_count": len(provenance),
            "report_overview": bool(originals)
            or any(s.get("prediction_pages") for s in summaries)
            or len(complete_tables) > len(blocks) - 1,
            "source_block_count": sum(len(p.blocks) for p in originals),
            "metric_definition": "m6: per-horizon horizontal-error MAE/RMSE in metres; not trajectory ADE/FDE",
            "common_sample_definition": "intersection of successful trial sample keys",
        },
        blocks=blocks,
    )
    if pages is not None:
        if len(complete_tables) > len(blocks) - 1:
            all_blocks = [blocks[0], *complete_tables]
            for offset in range(0, len(all_blocks), 20):
                pages.append(
                    analysis.model_copy(
                        update={
                            "blocks": all_blocks[offset : offset + 20],
                            "context": {
                                **analysis.context,
                                "report_overview": False,
                                "page_number": offset // 20 + 1,
                            },
                        }
                    ).model_dump(mode="json")
                )
        pages.extend(original.model_dump(mode="json") for original in originals)
        pages.extend(
            AnalysisResult.model_validate(page).model_dump(mode="json")
            for summary in summaries
            for page in summary.get("prediction_pages", [])
        )
        if len(pages) > 99:
            raise ValueError("report_page_ceiling_exceeded")
        analysis.context["report_page_count"] = len(pages)
        analysis.context["report_pages_sha256"] = digest(pages)
    return analysis.model_dump(mode="json"), status
