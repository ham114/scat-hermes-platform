"""Release deployment boundaries, no credential fanout and recoverable offline backup."""

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from tests.visualization_runtime.test_pipeline import pipeline as pipeline  # noqa: PLC0414
from visualization_runtime.acceptance import RemoteSandbox, synthetic
from visualization_runtime.release import backup, inspect_database, verify_backup

ROOT = Path(__file__).parents[2]


def test_opt_in_compose_routes_same_state_without_docker_socket_or_model_keys():
    spec = importlib.util.spec_from_file_location(
        "compose_generator", ROOT / "scripts/generate_compose_h7b.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.visualization_overlay()
    checked = json.loads((ROOT / "infra/compose/compose.visualization.yaml").read_text())
    assert result == checked
    worker = result["services"]["visualization-worker"]
    assert worker["profiles"] == ["visualization"] and worker["command"] == ["visualization-worker"]
    assert worker["environment"]["SCAT_VIS_ENABLED"] == "${SCAT_VIS_ENABLED:-false}"
    assert not any("docker.sock" in volume["source"] for volume in worker["volumes"])
    for name in ("platform", "hermes-main", "actions"):
        assert "env_file" not in result["services"][name]
        assert not any("API_KEY" in key for key in result["services"][name]["environment"])
        assert (
            result["services"][name]["environment"]["SCAT_VIS_ARTIFACT_ROOT"]
            == worker["environment"]["SCAT_VIS_ARTIFACT_ROOT"]
        )


def test_control_admission_does_not_require_execution_credentials(pipeline):
    from application.visualization_tasks import create_visualization_tasks
    from tests.visualization_runtime.test_delivery import parent
    from visualization_runtime.contracts import CreateVisualization
    from visualization_runtime.settings import VisualizationSettings

    app, context, job, *_ = pipeline
    path = parent(app, context)
    control = create_visualization_tasks(
        app.jobs.workspace_root,
        app.jobs.path,
        VisualizationSettings(
            enabled=True, artifact_root="artifacts", sandbox_image="sha256:" + "1" * 64
        ),
        app.receipts,
        parent_budget_path=path,
        read_only=True,
        execution_credentials=False,
    )
    refs = app.require_snapshot(context, job["task_id"])["payload"]["datasets"][0]["reference"]
    created = asyncio.run(
        control.create(
            context,
            CreateVisualization(request_id="control", intent="draw", data_refs=[refs]),
            parent_run_id="run_fixture",
        )
    )
    assert created["state"] == "queued"


def test_quiesced_backup_preserves_sqlite_and_historical_artifacts(pipeline):
    app, _context, _, worker, *_ = pipeline
    asyncio.run(worker.run_once())
    destination = app.jobs.workspace_root / "backup"
    result = backup(
        app.jobs.workspace_root, destination, {"tasks": app.jobs.path}, app.snapshots.root
    )
    assert "tasks.sqlite" in result["files"]
    assert any(k.endswith("desktop.png") for k in result["files"])
    with sqlite3.connect(destination / "tasks.sqlite") as db:
        assert db.execute("SELECT count(*) FROM visualization_publications").fetchone()[0] == 1
    assert inspect_database(destination / "tasks.sqlite")
    assert verify_backup(app.jobs.workspace_root, destination)
    with pytest.raises(ValueError, match="destination_exists"):
        backup(app.jobs.workspace_root, destination, {"tasks": app.jobs.path}, app.snapshots.root)
    (destination / "tasks.sqlite").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity_failed"):
        verify_backup(app.jobs.workspace_root, destination)


def test_backup_refuses_queued_or_unknown_jobs_and_creates_no_partial_output(pipeline):
    app, *_ = pipeline
    destination = app.jobs.workspace_root / "backup"
    with pytest.raises(ValueError, match="not_drained"):
        backup(app.jobs.workspace_root, destination, {"tasks": app.jobs.path}, app.snapshots.root)
    assert not destination.exists()


def test_release_rejects_wal_shared_database(tmp_path):
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE example(a)")
    with pytest.raises(ValueError, match="rollback_journal"):
        inspect_database(path)


def test_pre_feature_backup_keeps_legacy_state_without_creating_visualization_tables(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE model_tasks(id TEXT)")
        db.execute("INSERT INTO model_tasks VALUES('preserved')")
    destination = tmp_path / "backup"
    backup(tmp_path, destination, {"tasks": path}, tmp_path / "not-created-yet")
    assert verify_backup(tmp_path, destination)
    assert inspect_database(path) == {"model_tasks"}


def test_backup_cannot_omit_existing_artifact_history(pipeline):
    app, _, _, worker, *_ = pipeline
    asyncio.run(worker.run_once())
    with pytest.raises(ValueError, match="artifacts_missing"):
        backup(
            app.jobs.workspace_root,
            app.jobs.workspace_root / "backup",
            {"tasks": app.jobs.path},
            app.jobs.workspace_root / "missing",
        )
    wrong = app.jobs.workspace_root / "wrong"
    wrong.mkdir()
    with pytest.raises(ValueError, match="artifact_reference_missing"):
        backup(
            app.jobs.workspace_root,
            app.jobs.workspace_root / "backup",
            {"tasks": app.jobs.path},
            wrong,
        )


def test_acceptance_frozen_plan_covers_twelve_separate_cases_and_no_hidden_answers():
    plan = json.loads(
        (ROOT / "config/visualization-acceptance-v1.json").read_text(encoding="utf-8")
    )
    assert len(plan["cases"]) == len({case["id"] for case in plan["cases"]}) == 12
    assert sum(bool(case.get("defect")) for case in plan["cases"]) == 2
    assert synthetic()["synthetic"] is True
    assert any(row["temperature"] is None for row in synthetic()["data"]["points"])
    assert synthetic(True)["data"]["points"] == []


def test_acceptance_remote_transport_is_confined_to_dedicated_workspace():
    with pytest.raises(ValueError, match="isolated_p6"):
        RemoteSandbox("sha256:" + "1" * 64, "example", "/production", "/python")


def test_synthetic_acceptance_uses_owner_scoped_fixture_artifact(pipeline):
    from visualization_runtime.acceptance import SyntheticArtifacts
    from visualization_runtime.snapshots import digest

    app, context, _, *_ = pipeline
    fixtures = SyntheticArtifacts()
    app.artifacts = fixtures
    ref = fixtures.add(context, "unit", synthetic())
    job = asyncio.run(
        app.prepare(
            context,
            "fixture-artifact",
            intent="模拟数据",
            references=[ref],
            preferences={},
            preferences_sha256=digest({}),
        )
    )
    dataset = app.require_snapshot(context, job["task_id"])["payload"]["datasets"][0]
    assert dataset["operation"] == "p6_synthetic_fixture"
    assert dataset["payload"]["synthetic"] is True
    assert asyncio.run(fixtures.get_for_owner(ref["source_id"], owner_user_id="other")) is None


def test_unreachable_launcher_keeps_task_fenced_and_worker_alive(pipeline, monkeypatch):
    from visualization_runtime.sandbox import RenderError

    app, context, job, worker, calls, *_, sandbox = pipeline

    def unavailable(*args):
        raise RenderError("sandbox_service_unavailable")

    monkeypatch.setattr(sandbox, "render", unavailable)
    monkeypatch.setattr(sandbox, "remove", unavailable)
    monkeypatch.setattr(sandbox, "stopped", lambda _: False)
    assert asyncio.run(worker.run_once()) is True
    assert app.jobs.get(context, job["task_id"])["state"] == "needs_reconciliation"
    assert calls == {"code": 1, "review": 0}
    assert asyncio.run(worker.run_once()) is False
    assert calls == {"code": 1, "review": 0}


def test_disabled_worker_never_claims_or_calls_providers(pipeline):
    app, context, job, worker, calls, *_ = pipeline
    worker.settings = worker.settings.model_copy(update={"enabled": False})
    assert asyncio.run(worker.run_once()) is False
    assert app.jobs.get(context, job["task_id"])["state"] == "queued"
    assert calls == {"code": 0, "review": 0}


def test_malformed_review_is_named_and_not_retried_or_published(pipeline):
    app, context, job, worker, calls, reports, *_ = pipeline
    reports[:] = ['{"audit":{},"summary":"looks good","issues":[]}']
    asyncio.run(worker.run_once())
    current = app.jobs.get(context, job["task_id"])
    assert current["state"] == "failed" and current["error_code"] == "review_contract_invalid"
    assert calls == {"code": 1, "review": 1}
    with app.jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_publications").fetchone()[0] == 0
