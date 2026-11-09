"""M4-08 物化任务命令行入口测试（不连接数据库）。"""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock

import pytest

from scat.materialization import cli
from scat.materialization.tasks import (
    FLIGHT_FEATURES_ALGORITHM_VERSION,
    FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION,
)


def test_progress_reporter_is_awaitable():
    """任务层以 await 调用进度回调，CLI 的回调必须是协程函数。"""
    assert inspect.iscoroutinefunction(cli._report)


def test_parser_defaults_for_flight_features():
    args = cli.build_parser().parse_args(["flight-features", "--batch", "scat20161015_20161021"])

    assert args.command == "flight-features"
    assert args.batch == "scat20161015_20161021"
    assert args.external_ids is None
    assert args.retry_job is None
    assert args.force is False
    assert args.limit is None
    assert args.page_size == 200
    assert args.algorithm_version == FLIGHT_FEATURES_ALGORITHM_VERSION


def test_parser_defaults_for_phase_segments():
    """M4-12：段级阶段标注子命令的默认值与航班特征命令一致（算法版本除外）。"""
    args = cli.build_parser().parse_args(["phase-segments", "--batch", "scat20161015_20161021"])

    assert args.command == "phase-segments"
    assert args.batch == "scat20161015_20161021"
    assert args.external_ids is None
    assert args.retry_job is None
    assert args.force is False
    assert args.rebuild is False
    assert args.limit is None
    assert args.page_size == 200
    assert args.algorithm_version == FLIGHT_PHASE_SEGMENTS_ALGORITHM_VERSION


def test_parser_rejects_conflicting_phase_segments_flags():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["phase-segments", "--batch", "b", "--external-id", "1", "--retry-job", "job"]
    )

    with pytest.raises(SystemExit):
        cli._validate(parser, args)


def test_parser_accepts_repeated_external_ids_and_flags():
    args = cli.build_parser().parse_args(
        [
            "flight-features",
            "--batch",
            "b",
            "--external-id",
            "100002",
            "--external-id",
            "100003",
            "--force",
            "--limit",
            "10",
        ]
    )

    assert args.external_ids == ["100002", "100003"]
    assert args.force is True
    assert args.limit == 10


def test_parser_requires_batch():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["flight-features"])


def test_parser_rejects_conflicting_targets():
    with pytest.raises(SystemExit):
        cli.main(
            [
                "flight-features",
                "--batch",
                "b",
                "--external-id",
                "100002",
                "--retry-job",
                "job-1",
            ]
        )


def test_parser_rejects_non_positive_limit():
    with pytest.raises(SystemExit):
        cli.main(["flight-features", "--batch", "b", "--limit", "0"])


def test_parser_rejects_rebuild_with_targets_or_retry_job():
    with pytest.raises(SystemExit):
        cli.main(
            [
                "flight-features",
                "--batch",
                "b",
                "--rebuild",
                "--external-id",
                "100002",
            ]
        )
    with pytest.raises(SystemExit):
        cli.main(["flight-features", "--batch", "b", "--rebuild", "--retry-job", "job-1"])


def test_parser_supports_maintenance_commands():
    parser = cli.build_parser()

    indexes = parser.parse_args(["ensure-indexes"])
    assert indexes.command == "ensure-indexes"

    cleanup = parser.parse_args(["cleanup", "--kind", "flight_features", "--batch", "b", "--apply"])
    assert cleanup.command == "cleanup"
    assert cleanup.kind == "flight_features"
    assert cleanup.apply is True
    # 缺省 dry-run：不传 --apply 时不得删除
    assert parser.parse_args(["cleanup"]).apply is False
    assert parser.parse_args(["flight-features", "--batch", "b"]).rebuild is False


def test_parser_rejects_unknown_cleanup_kind():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["cleanup", "--kind", "jobs"])


def test_main_returns_zero_for_cleanup_dry_run(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_execute",
        AsyncMock(
            return_value={
                "status": "succeeded",
                "dry_run": True,
                "deleted_total": 0,
            }
        ),
    )

    assert cli.main(["cleanup"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["dry_run"] is True


def test_main_reports_failure_as_json(capsys, monkeypatch):
    async def boom(args):  # pragma: no cover - 只验证 CLI 边界
        raise ValueError("批次不存在或无数据: b")

    monkeypatch.setattr(cli, "_execute", boom)

    exit_code = cli.main(["batch-statistics", "--batch", "b"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "failed"
    assert "批次不存在或无数据" in payload["error"]


def test_main_returns_zero_for_partial_success(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_execute",
        AsyncMock(return_value={"status": "partial", "succeeded_count": 1}),
    )

    assert cli.main(["flight-features", "--batch", "b"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "partial"
