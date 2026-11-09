"""M4-08 物化任务命令行入口（后台队列按需再引入）。

在服务器 `scat-agent` 环境中于 `src/` 目录执行：

    python -m scat.materialization.cli batch-statistics --batch scat20161015_20161021
    python -m scat.materialization.cli flight-features --batch scat20161015_20161021 --limit 200
    python -m scat.materialization.cli flight-features --batch B --external-id 100002 --force
    python -m scat.materialization.cli flight-features --batch B --retry-job <job_id>

- 默认按增量执行：跳过已有相同 `source_version + algorithm_version` 结果的航班，
  因此重复执行或中断后重跑只补算缺失部分；`--force` 强制重算；
- 每个任务在 `scat_materialization_jobs` 中留下状态、进度、失败单位与原因；
- 标准输出为单行 JSON 摘要；`--progress` 时进度写到标准错误。
  退出码：0=成功或部分成功，1=任务失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from core import settings
from core.observability import configure_structured_logging
from scat.infrastructure.database import flight_database
from scat.materialization.contracts import MaterializationJobRecord
from scat.materialization.jobs import (
    DEFAULT_PROGRESS_INTERVAL,
    run_batch_statistics_job,
    run_flight_features_job,
    run_flight_phase_segments_job,
    run_prediction_metric_summaries_job,
    run_prediction_metrics_job,
)
from scat.materialization.maintenance import (
    MATERIALIZED_KINDS,
    cleanup_stale_versions,
    purge_batch_version,
)
from scat.materialization.repository import MaterializationRepository
from scat.materialization.tasks import (
    BATCH_STATISTICS_ALGORITHM_VERSION,
    DEFAULT_SOURCE_VERSION,
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
    PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION,
    PREDICTION_METRICS_ALGORITHM_VERSION,
)
from scat.performance import ensure_query_indexes
from scat.repositories.flight_repository import ScatFlightRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scat.materialization.cli",
        description="SCAT 派生数据物化任务（只读原始集合，只写派生集合）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    statistics = subparsers.add_parser("batch-statistics", help="物化批次统计")
    statistics.add_argument("--batch", required=True, help="SCAT 批次标识")
    statistics.add_argument("--job-id", default=None, help="自定义任务 ID（默认自动生成）")
    statistics.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    statistics.add_argument("--algorithm-version", default=BATCH_STATISTICS_ALGORITHM_VERSION)
    statistics.add_argument("--progress", action="store_true", help="进度写入标准错误")

    features = subparsers.add_parser("flight-features", help="物化单航班特征")
    features.add_argument("--batch", required=True, help="SCAT 批次标识")
    features.add_argument(
        "--external-id",
        action="append",
        dest="external_ids",
        default=None,
        help="只处理指定航班（可重复）；缺省处理整批",
    )
    features.add_argument("--retry-job", default=None, help="只重算该任务失败的单位")
    features.add_argument("--force", action="store_true", help="强制重算（忽略已有结果）")
    features.add_argument(
        "--rebuild",
        action="store_true",
        help="先删除该批次当前版本的派生结果再重算（M4-10 可重建）",
    )
    features.add_argument("--limit", type=int, default=None, help="本次最多处理的航班数")
    features.add_argument("--page-size", type=int, default=200, help="游标分页大小")
    features.add_argument(
        "--concurrency",
        type=int,
        default=settings.SCAT_MATERIALIZATION_CONCURRENCY,
        help="单航班受控并发数（1-16）",
    )
    features.add_argument("--job-id", default=None, help="自定义任务 ID（默认自动生成）")
    features.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    features.add_argument("--algorithm-version", default=FLIGHT_FEATURES_ALGORITHM_VERSION)
    features.add_argument("--progress-interval", type=int, default=DEFAULT_PROGRESS_INTERVAL)
    features.add_argument("--progress", action="store_true", help="进度写入标准错误")

    phase_segments = subparsers.add_parser(
        "phase-segments", help="物化段级阶段标注（论文口径 RDP + 六类）"
    )
    phase_segments.add_argument("--batch", required=True, help="SCAT 批次标识")
    phase_segments.add_argument(
        "--external-id",
        action="append",
        dest="external_ids",
        default=None,
        help="只处理指定航班（可重复）；缺省处理整批",
    )
    phase_segments.add_argument("--retry-job", default=None, help="只重算该任务失败的单位")
    phase_segments.add_argument("--force", action="store_true", help="强制重算（忽略已有结果）")
    phase_segments.add_argument(
        "--rebuild",
        action="store_true",
        help="先删除该批次当前版本的派生结果再重算（M4-10 可重建）",
    )
    phase_segments.add_argument("--limit", type=int, default=None, help="本次最多处理的航班数")
    phase_segments.add_argument("--page-size", type=int, default=200, help="游标分页大小")
    phase_segments.add_argument(
        "--concurrency",
        type=int,
        default=settings.SCAT_MATERIALIZATION_CONCURRENCY,
        help="单航班受控并发数（1-16）",
    )
    phase_segments.add_argument("--job-id", default=None, help="自定义任务 ID（默认自动生成）")
    phase_segments.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    phase_segments.add_argument(
        "--algorithm-version", default=FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION
    )
    phase_segments.add_argument("--progress-interval", type=int, default=DEFAULT_PROGRESS_INTERVAL)
    phase_segments.add_argument("--progress", action="store_true", help="进度写入标准错误")

    prediction_metrics = subparsers.add_parser(
        "prediction-metrics", help="物化预测指标（M5 评估口径，见 docs/PREDICTION_EVALUATION.md）"
    )
    prediction_metrics.add_argument("--batch", required=True, help="SCAT 批次标识")
    prediction_metrics.add_argument(
        "--external-id",
        action="append",
        dest="external_ids",
        default=None,
        help="只处理指定航班（可重复）；缺省处理整批",
    )
    prediction_metrics.add_argument("--retry-job", default=None, help="只重算该任务失败的单位")
    prediction_metrics.add_argument("--force", action="store_true", help="强制重算（忽略已有结果）")
    prediction_metrics.add_argument(
        "--rebuild",
        action="store_true",
        help="先删除该批次当前版本的派生结果再重算（M4-10 可重建）",
    )
    prediction_metrics.add_argument("--limit", type=int, default=None, help="本次最多处理的航班数")
    prediction_metrics.add_argument("--page-size", type=int, default=200, help="游标分页大小")
    prediction_metrics.add_argument(
        "--concurrency",
        type=int,
        default=settings.SCAT_MATERIALIZATION_CONCURRENCY,
        help="单航班受控并发数（1-16）",
    )
    prediction_metrics.add_argument("--job-id", default=None, help="自定义任务 ID（默认自动生成）")
    prediction_metrics.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    prediction_metrics.add_argument(
        "--algorithm-version", default=PREDICTION_METRICS_ALGORITHM_VERSION
    )
    prediction_metrics.add_argument(
        "--progress-interval", type=int, default=DEFAULT_PROGRESS_INTERVAL
    )
    prediction_metrics.add_argument("--progress", action="store_true", help="进度写入标准错误")

    metric_summaries = subparsers.add_parser(
        "metric-summaries",
        help="按批次聚合预测指标汇总（M5-06：overall/phase/aircraft_type × 时域）",
    )
    metric_summaries.add_argument("--batch", required=True, help="SCAT 批次标识")
    metric_summaries.add_argument("--job-id", default=None, help="自定义任务 ID（默认自动生成）")
    metric_summaries.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    metric_summaries.add_argument(
        "--algorithm-version", default=PREDICTION_METRIC_SUMMARIES_ALGORITHM_VERSION
    )
    metric_summaries.add_argument("--progress", action="store_true", help="进度写入标准错误")

    indexes = subparsers.add_parser("ensure-indexes", help="创建派生集合索引（幂等）")
    indexes.add_argument("--progress", action="store_true", help="进度写入标准错误")

    cleanup = subparsers.add_parser("cleanup", help="清理旧版本派生结果（默认 dry-run）")
    cleanup.add_argument(
        "--kind",
        choices=sorted(MATERIALIZED_KINDS),
        default=None,
        help="只清理指定派生集合；缺省清理全部已实现物化的集合",
    )
    cleanup.add_argument("--batch", default=None, help="只清理指定批次")
    cleanup.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="实际执行删除（缺省只统计匹配数量）",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "cleanup":
        return
    if args.command not in {"flight-features", "phase-segments", "prediction-metrics"}:
        return
    if args.external_ids and args.retry_job:
        parser.error("--external-id 与 --retry-job 不能同时使用")
    if args.external_ids and args.rebuild:
        parser.error("--external-id 与 --rebuild 不能同时使用（rebuild 会删除整批当前版本结果）")
    if args.retry_job and args.rebuild:
        parser.error("--retry-job 与 --rebuild 不能同时使用")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.page_size < 1:
        parser.error("--page-size 必须大于 0")
    if not 1 <= args.concurrency <= 16:
        parser.error("--concurrency 必须在 1 到 16 之间")
    if args.progress_interval < 1:
        parser.error("--progress-interval 必须大于 0")


async def _report(record: MaterializationJobRecord) -> None:
    print(
        f"[materialize] {record.job_id} {record.status} "
        f"{record.processed_count}/{record.total_count} "
        f"progress={record.progress:.1%} failed={len(record.failed_targets)}",
        file=sys.stderr,
        flush=True,
    )


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    await flight_database.connect()
    try:
        flight_repository = ScatFlightRepository()
        materialization_repository = MaterializationRepository(flight_database.db)

        if args.command == "ensure-indexes":
            created = [
                *await materialization_repository.ensure_indexes(),
                *await ensure_query_indexes(flight_database.db),
            ]
            return {"status": "succeeded", "kind": "ensure_indexes", "indexes": created}

        if args.command == "cleanup":
            report = await cleanup_stale_versions(
                materialization_repository,
                source_version=args.source_version,
                kind=args.kind,
                batch=args.batch,
                dry_run=not args.apply,
            )
            return {"status": "succeeded", **report}

        if args.command == "batch-statistics":
            outcome = await run_batch_statistics_job(
                batch=args.batch,
                flight_repository=flight_repository,
                materialization_repository=materialization_repository,
                job_id=args.job_id,
                source_version=args.source_version,
                algorithm_version=args.algorithm_version,
                on_progress=_report if args.progress else None,
            )
        elif args.command == "metric-summaries":
            outcome = await run_prediction_metric_summaries_job(
                batch=args.batch,
                materialization_repository=materialization_repository,
                job_id=args.job_id,
                source_version=args.source_version,
                algorithm_version=args.algorithm_version,
                on_progress=_report if args.progress else None,
            )
        elif args.command == "phase-segments":
            purged_count = 0
            if args.rebuild:
                # M4-10：先删除该批次当前版本结果，再按同一口径重建
                purged_count = await purge_batch_version(
                    materialization_repository,
                    kind="phase_segments",
                    batch=args.batch,
                    source_version=args.source_version,
                    algorithm_version=args.algorithm_version,
                )
            outcome = await run_flight_phase_segments_job(
                batch=args.batch,
                flight_repository=flight_repository,
                materialization_repository=materialization_repository,
                external_ids=args.external_ids,
                retry_of=args.retry_job,
                force=args.force,
                limit=args.limit,
                page_size=args.page_size,
                concurrency=args.concurrency,
                job_id=args.job_id,
                source_version=args.source_version,
                algorithm_version=args.algorithm_version,
                progress_interval=args.progress_interval,
                on_progress=_report if args.progress else None,
            )
            summary = outcome.as_dict()
            summary["purged_count"] = purged_count
            return summary
        elif args.command == "prediction-metrics":
            purged_count = 0
            if args.rebuild:
                purged_count = await purge_batch_version(
                    materialization_repository,
                    kind="prediction_metrics",
                    batch=args.batch,
                    source_version=args.source_version,
                    algorithm_version=args.algorithm_version,
                )
            outcome = await run_prediction_metrics_job(
                batch=args.batch,
                flight_repository=flight_repository,
                materialization_repository=materialization_repository,
                external_ids=args.external_ids,
                retry_of=args.retry_job,
                force=args.force,
                limit=args.limit,
                page_size=args.page_size,
                concurrency=args.concurrency,
                job_id=args.job_id,
                source_version=args.source_version,
                algorithm_version=args.algorithm_version,
                progress_interval=args.progress_interval,
                on_progress=_report if args.progress else None,
            )
            summary = outcome.as_dict()
            summary["purged_count"] = purged_count
            return summary
        else:
            purged_count = 0
            if args.rebuild:
                # M4-10：先删除该批次当前版本结果，再按同一口径重建
                purged_count = await purge_batch_version(
                    materialization_repository,
                    kind="flight_features",
                    batch=args.batch,
                    source_version=args.source_version,
                    algorithm_version=args.algorithm_version,
                )
            outcome = await run_flight_features_job(
                batch=args.batch,
                flight_repository=flight_repository,
                materialization_repository=materialization_repository,
                external_ids=args.external_ids,
                retry_of=args.retry_job,
                force=args.force,
                limit=args.limit,
                page_size=args.page_size,
                concurrency=args.concurrency,
                job_id=args.job_id,
                source_version=args.source_version,
                algorithm_version=args.algorithm_version,
                progress_interval=args.progress_interval,
                on_progress=_report if args.progress else None,
            )
            summary = outcome.as_dict()
            summary["purged_count"] = purged_count
            return summary
        return outcome.as_dict()
    finally:
        await flight_database.close()


def main(argv: Sequence[str] | None = None) -> int:
    configure_structured_logging(
        level=settings.LOG_LEVEL.to_logging_level(),
        json_logs=settings.OBSERVABILITY_JSON_LOGS,
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    try:
        summary = asyncio.run(_execute(args))
    except Exception as error:  # noqa: BLE001 - CLI 边界：稳定错误输出与退出码
        print(
            json.dumps(
                {"status": "failed", "error": str(error)[:500]},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if summary.get("status") in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
