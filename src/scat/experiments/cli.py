"""M6 实验 CLI：实验的提交、领取、运行与台账查询（M6-06/07/08）。

用法（项目根目录、`PYTHONPATH=src` 环境下）：

    python -m scat.experiments.cli run --batch scat20161015_20161021 \
        --predictor constant-velocity-heading --limit 100 --progress
    python -m scat.experiments.cli submit --batch scat20161015_20161021 \
        --predictor kalman-cv --limit 200
    python -m scat.experiments.cli claim --progress           # 领取最早的 pending 实验并执行
    python -m scat.experiments.cli claim --experiment-id m6eval-...
    python -m scat.experiments.cli requeue --experiment-id m6eval-... \
        --operator ops-user --reason "worker interrupted"
    python -m scat.experiments.cli list --batch scat20161015_20161021 --limit 10
    python -m scat.experiments.cli show --experiment-id m6eval-...
    python -m scat.experiments.cli export-report --experiment-id m6eval-...
    python -m scat.experiments.cli promote-check --candidate-predictor kalman-cv \
        --incumbent-predictor constant-velocity-heading
    python -m scat.experiments.cli ensure-indexes

约定：stdout 单行 JSON；`--progress` 时进度写 stderr；退出码 0=成功/部分成功，1=失败。
`submit` 只落 `pending` 记录；执行由部署者配置的平台执行器领取，
定时领取应使用进程锁串行执行，日志路径由部署环境指定；也可手动 claim。
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
from scat.experiments.contracts import ExperimentRecord
from scat.experiments.evaluation import EvaluationConfig
from scat.experiments.promotion import evaluate_promotion, select_latest_succeeded
from scat.experiments.repository import ExperimentRepository
from scat.experiments.runner import (
    create_pending_experiment,
    export_experiment_report,
    run_pending_experiment,
    run_predictor_evaluation,
)
from scat.infrastructure.database import flight_database
from scat.prediction.registry import default_registry
from scat.repositories.flight_repository import ScatFlightRepository

DEFAULT_BATCH = "scat20161015_20161021"
DEFAULT_HORIZONS = "60,180,300,600,900,1200"


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch", default=DEFAULT_BATCH, help="SCAT 批次标识")
    parser.add_argument("--predictor", default="kalman-cv", help="预测器 id")
    parser.add_argument("--limit", type=int, default=None, help="最多处理的航班数（缺省整批）")
    parser.add_argument("--stride-s", type=float, default=60.0, help="观测截止点采样步长（秒）")
    parser.add_argument(
        "--max-cutoffs-per-flight",
        type=int,
        default=12,
        help="单航班最多采样多少个截止点",
    )
    parser.add_argument("--min-observed-s", type=float, default=60.0, help="最短观测时长（秒）")
    parser.add_argument("--horizons", default=DEFAULT_HORIZONS, help="逗号分隔的时域（秒）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scat.experiments.cli",
        description="M6 自研模型实验：提交/领取/运行与台账查询（只读原始数据，写 scat_model_experiments）。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="立即运行一次预测器评估实验并存档")
    _add_run_arguments(run)
    run.add_argument("--page-size", type=int, default=200, help="航班游标分页大小")
    run.add_argument("--progress", action="store_true", help="进度写入 stderr")
    run.add_argument("--progress-interval", type=int, default=20, help="每处理多少航班输出一次进度")

    submit = subparsers.add_parser("submit", help="提交一条 pending 实验（不执行计算）")
    _add_run_arguments(submit)

    claim = subparsers.add_parser("claim", help="领取一条 pending 实验并执行（运维执行器）")
    claim.add_argument("--experiment-id", default=None, help="指定实验 id；缺省领取最早创建的一条")
    claim.add_argument("--page-size", type=int, default=200, help="航班游标分页大小")
    claim.add_argument("--progress", action="store_true", help="进度写入 stderr")
    claim.add_argument(
        "--progress-interval", type=int, default=20, help="每处理多少航班输出一次进度"
    )

    requeue = subparsers.add_parser(
        "requeue",
        help="人工重新入队一条租约已过期或 failed 的实验（带退避、次数上限和审计）",
    )
    requeue.add_argument("--experiment-id", required=True)
    requeue.add_argument("--operator", required=True, help="执行本次恢复操作的稳定操作者标识")
    requeue.add_argument("--reason", required=True, help="重新入队原因（写入审计记录）")

    listing = subparsers.add_parser("list", help="列出最近实验（摘要）")
    listing.add_argument("--batch", default=None)
    listing.add_argument("--predictor", default=None)
    listing.add_argument("--limit", type=int, default=10)

    show = subparsers.add_parser("show", help="按 experiment_id 查看完整记录")
    show.add_argument("--experiment-id", required=True)

    export = subparsers.add_parser("export-report", help="把实验报告写入产物目录并登记到记录")
    export.add_argument("--experiment-id", required=True)

    promote = subparsers.add_parser(
        "promote-check", help="按准入门槛对比候选与现任模型（只读，M6-10）"
    )
    promote.add_argument("--candidate-predictor", required=True)
    promote.add_argument("--incumbent-predictor", required=True)
    promote.add_argument("--batch", default=DEFAULT_BATCH)
    promote.add_argument("--candidate-experiment-id", default=None)
    promote.add_argument("--incumbent-experiment-id", default=None)

    subparsers.add_parser("ensure-indexes", help="创建实验集合索引（幂等）")
    return parser


def _parse_horizons(parser: argparse.ArgumentParser, text: str) -> tuple[float, ...]:
    try:
        horizons = tuple(float(value) for value in text.split(",") if value.strip())
    except ValueError:
        parser.error("--horizons 必须是逗号分隔的数字")
    if not horizons or any(value <= 0 for value in horizons):
        parser.error("--horizons 必须为正数")
    return tuple(sorted(set(horizons)))


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command in {"run", "submit", "claim"}:
        if getattr(args, "limit", None) is not None and args.limit < 1:
            parser.error("--limit 必须大于 0")
        if args.command in {"run", "submit"}:
            if args.stride_s <= 0:
                parser.error("--stride-s 必须大于 0")
            if args.min_observed_s <= 0:
                parser.error("--min-observed-s 必须大于 0")
            if args.max_cutoffs_per_flight < 1:
                parser.error("--max-cutoffs-per-flight 必须大于 0")
            args.horizon_values = _parse_horizons(parser, args.horizons)
            try:
                default_registry.get(args.predictor)
            except KeyError:
                available = ", ".join(pid for pid, _version in default_registry.list_predictors())
                parser.error(f"未知预测器: {args.predictor}（可用: {available}）")
        if args.command in {"run", "claim"}:
            if args.page_size < 1:
                parser.error("--page-size 必须大于 0")
            if args.progress_interval < 1:
                parser.error("--progress-interval 必须大于 0")
        return
    if args.command == "list" and args.limit < 1:
        parser.error("--limit 必须大于 0")


async def _report(progress: dict[str, Any]) -> None:
    print(
        f"[experiments] processed={progress['processed']} "
        f"samples={progress['samples']} failed={progress['failed']}",
        file=sys.stderr,
        flush=True,
    )


def _config_from_args(args: argparse.Namespace) -> EvaluationConfig:
    return EvaluationConfig(
        batch=args.batch,
        predictor_id=args.predictor,
        horizons_s=args.horizon_values,
        stride_s=args.stride_s,
        max_cutoffs_per_flight=args.max_cutoffs_per_flight,
        min_observed_s=args.min_observed_s,
    )


def _run_summary(record: Any) -> dict[str, Any]:
    return {
        "status": record.status,
        "experiment_id": record.experiment_id,
        "predictor_id": record.predictor_id,
        "data_version": record.data_version,
        "coverage": record.coverage,
        "metrics": [item.model_dump(mode="json") for item in record.metrics],
    }


async def _resolve_experiment(
    repository: ExperimentRepository,
    *,
    experiment_id: str | None,
    predictor_id: str,
    batch: str | None,
) -> dict[str, Any] | None:
    """按 experiment_id 直取，或按“最新 succeeded 实验”解析（promote-check 用）。"""
    if experiment_id:
        document = await repository.get(experiment_id)
        if document is None:
            return None
        return {key: value for key, value in document.items() if key != "_id"}
    rows = await repository.list_with_metrics(batch=batch, predictor_id=predictor_id, limit=50)
    return select_latest_succeeded(rows)


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    await flight_database.connect()
    try:
        repository = ExperimentRepository(flight_database.db)

        if args.command == "ensure-indexes":
            created = await repository.ensure_indexes()
            return {"status": "succeeded", "kind": "ensure_indexes", "indexes": created}

        if args.command == "list":
            rows = await repository.list_recent(
                batch=args.batch,
                predictor_id=args.predictor,
                limit=args.limit,
            )
            return {"status": "succeeded", "count": len(rows), "experiments": rows}

        if args.command == "show":
            document = await repository.get(args.experiment_id)
            if document is None:
                return {"status": "not_found", "experiment_id": args.experiment_id}
            document.pop("_id", None)
            return {"status": "succeeded", "experiment": document}

        if args.command == "export-report":
            document = await repository.get(args.experiment_id)
            if document is None:
                return {"status": "not_found", "experiment_id": args.experiment_id}
            payload = {key: value for key, value in document.items() if key != "_id"}
            record = ExperimentRecord.model_validate(payload)
            entry = export_experiment_report(record)
            record.artifacts = [
                item for item in record.artifacts if item.get("name") != f"{entry.get('name', '')}"
            ] + [entry]
            await repository.upsert(record)
            return {
                "status": "succeeded",
                "experiment_id": record.experiment_id,
                "artifact": entry,
            }

        if args.command == "promote-check":
            candidate = await _resolve_experiment(
                repository,
                experiment_id=args.candidate_experiment_id,
                predictor_id=args.candidate_predictor,
                batch=args.batch,
            )
            incumbent = await _resolve_experiment(
                repository,
                experiment_id=args.incumbent_experiment_id,
                predictor_id=args.incumbent_predictor,
                batch=args.batch,
            )
            missing = [
                name
                for name, record in (("candidate", candidate), ("incumbent", incumbent))
                if record is None
            ]
            if missing:
                return {
                    "status": "not_found",
                    "missing": missing,
                    "message": "缺少可用的 succeeded 实验（候选/现任至少一方没有合格记录）。",
                }
            report = evaluate_promotion(candidate=candidate, incumbent=incumbent)
            return {
                "status": "succeeded" if report["passed"] else "gate_failed",
                "gate": report,
            }

        if args.command == "submit":
            record = await create_pending_experiment(
                config=_config_from_args(args),
                experiment_repository=repository,
                limit=args.limit,
                source="cli",
            )
            return {
                "status": "succeeded",
                "experiment_id": record.experiment_id,
                "experiment_status": record.status,
                "hint": (
                    "执行：python -m scat.experiments.cli claim "
                    f"--experiment-id {record.experiment_id}"
                ),
            }

        if args.command == "claim":
            record = await run_pending_experiment(
                experiment_repository=repository,
                flight_repository=ScatFlightRepository(),
                experiment_id=args.experiment_id,
                page_size=args.page_size,
                progress_interval=args.progress_interval,
                on_progress=_report if args.progress else None,
            )
            if record is None:
                return {
                    "status": "succeeded",
                    "claimed": None,
                    "message": "没有待执行的 pending 实验。",
                }
            return {"status": record.status, "claimed": _run_summary(record)}

        if args.command == "requeue":
            result = await repository.requeue(
                args.experiment_id,
                reason=args.reason,
                operator=args.operator,
                retry_base_seconds=float(settings.SCAT_EXPERIMENT_RETRY_BASE_SECONDS),
                retry_max_seconds=float(settings.SCAT_EXPERIMENT_RETRY_MAX_SECONDS),
                default_max_attempts=int(settings.SCAT_EXPERIMENT_MAX_ATTEMPTS),
            )
            outcome = str(result["outcome"])
            document = result.get("document") or {}
            status_by_outcome = {
                "requeued": "succeeded",
                "not_found": "not_found",
                "active_lease": "conflict",
                "invalid_status": "conflict",
                "retry_exhausted": "failed",
                "conflict": "conflict",
            }
            return {
                "status": status_by_outcome.get(outcome, "failed"),
                "outcome": outcome,
                "experiment_id": args.experiment_id,
                "experiment_status": document.get("status"),
                "attempt_count": document.get("attempt_count"),
                "max_attempts": document.get("max_attempts"),
                "next_attempt_at": document.get("next_attempt_at"),
                "requeue_count": document.get("requeue_count"),
            }

        record = await run_predictor_evaluation(
            config=_config_from_args(args),
            flight_repository=ScatFlightRepository(),
            experiment_repository=repository,
            limit=args.limit,
            page_size=args.page_size,
            progress_interval=args.progress_interval,
            on_progress=_report if args.progress else None,
        )
        return _run_summary(record)
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
