"""Real scoped receipts become immutable, bounded, independently retained inputs."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.context import ExecutionContext
from application.visualization_tasks import VisualizationTasks
from hermes_runtime.results import ResultNotFoundError, ToolResultStore
from visualization_runtime.contracts import SourceReference
from visualization_runtime.snapshots import SnapshotStore, SnapshotUnavailable, digest
from visualization_runtime.store import JobConflict, VisualizationJobStore


@pytest.fixture
def setup_snapshot(tmp_path):
    now = [1000.0]
    jobs = VisualizationJobStore(
        tmp_path / "tasks.sqlite", workspace_root=tmp_path, clock=lambda: now[0]
    )
    snapshots = SnapshotStore(jobs, tmp_path / "artifacts", workspace_root=tmp_path, ttl=100)
    receipts = ToolResultStore(tmp_path / "receipts.sqlite", workspace_root=tmp_path)
    context = ExecutionContext("alice", "thread-1")
    payload = {
        "status": "ok",
        "query": {"start": 10, "every_nth": 2},
        "data": {
            "pagination": {"start": 10, "has_more": True},
            "points": [{"x": 0, "altitude": 12}, {"x": 1, "altitude": None}],
            "unit": "m",
        },
    }
    stored = receipts.put(
        context.scope_key, "run_input", "call-1", "get_scat_track_points", payload
    )
    reference = SourceReference(
        kind="readonly_result",
        source_id=stored["result_id"],
        run_id="run_input",
        payload_sha256=stored["payload_sha256"],
    )
    return jobs, snapshots, receipts, context, reference, payload, now


def capture(setup, **kwargs):
    _, snapshots, receipts, context, ref, _, _ = setup
    return asyncio.run(snapshots.capture(context, "capture-1", (ref,), receipts=receipts, **kwargs))


def test_snapshot_preserves_values_units_missing_data_and_pagination(setup_snapshot):
    snapshot = capture(setup_snapshot)
    dataset = snapshot["payload"]["datasets"][0]
    assert dataset["payload"] == setup_snapshot[5]
    assert dataset["scope"] == "fetched_payload" and dataset["completeness"] == "not_asserted"
    assert dataset["array_lengths"]["/data/points"] == 2


def test_receipt_deletion_does_not_destroy_independent_snapshot(setup_snapshot):
    _, snapshots, receipts, context, ref, _, _ = setup_snapshot
    snapshot = capture(setup_snapshot)
    with receipts._connect() as db:
        db.execute("DELETE FROM tool_results")
    assert snapshots.load(context, snapshot["snapshot_id"])["payload"] == snapshot["payload"]
    assert capture(setup_snapshot)["snapshot_id"] == snapshot["snapshot_id"]
    with pytest.raises(ResultNotFoundError):
        asyncio.run(snapshots.capture(context, "new-capture", (ref,), receipts=receipts))


@pytest.mark.parametrize(
    "other", [ExecutionContext("bob", "thread-1"), ExecutionContext("alice", "other")]
)
def test_cross_identity_cannot_capture_or_load(setup_snapshot, other):
    _, snapshots, receipts, _, ref, _, _ = setup_snapshot
    snapshot = capture(setup_snapshot)
    with pytest.raises(ResultNotFoundError):
        asyncio.run(snapshots.capture(other, "other", (ref,), receipts=receipts))
    with pytest.raises(SnapshotUnavailable):
        snapshots.load(other, snapshot["snapshot_id"])


def test_changed_reference_and_corrupt_snapshot_fail_closed(setup_snapshot):
    _, snapshots, receipts, context, ref, _, _ = setup_snapshot
    changed = ref.model_copy(update={"payload_sha256": "e" * 64})
    with pytest.raises(JobConflict, match="source_changed"):
        asyncio.run(snapshots.capture(context, "bad", (changed,), receipts=receipts))
    snapshot = capture(setup_snapshot)
    with pytest.raises(JobConflict, match="request_id_conflict"):
        asyncio.run(snapshots.capture(context, "capture-1", (changed,), receipts=receipts))
    with pytest.raises(JobConflict, match="reference_changed"):
        snapshots.load(context, snapshot["snapshot_id"], "e" * 64)
    snapshots._path(snapshot["snapshot_id"]).write_text("{}", encoding="utf-8")
    with pytest.raises(JobConflict, match="integrity_failed"):
        snapshots.load(context, snapshot["snapshot_id"])


def test_source_revocation_blocks_existing_and_future_materialization(setup_snapshot):
    _, snapshots, receipts, context, ref, _, _ = setup_snapshot
    snapshot = capture(setup_snapshot)
    assert snapshots.revoke_source(context, ref.kind, ref.source_id) == 1
    with pytest.raises(SnapshotUnavailable):
        snapshots.load(context, snapshot["snapshot_id"])
    with pytest.raises(SnapshotUnavailable, match="source_revoked"):
        asyncio.run(snapshots.capture(context, "new-request", (ref,), receipts=receipts))
    assert len(list((snapshots.root / "snapshots").glob("*.json"))) == 1


def test_expiry_and_explicit_revoke_deny_read(setup_snapshot):
    _, snapshots, _, context, _, _, now = setup_snapshot
    snapshot = capture(setup_snapshot)
    now[0] += 101
    with pytest.raises(SnapshotUnavailable):
        snapshots.load(context, snapshot["snapshot_id"])
    now[0] -= 101
    snapshots.revoke(context, snapshot["snapshot_id"])
    with pytest.raises(SnapshotUnavailable):
        snapshots.load(context, snapshot["snapshot_id"])


@pytest.mark.parametrize("limit,value", [("max_rows", 1), ("max_bytes", 10)])
def test_materialization_limits_leave_no_registered_or_partial_file(setup_snapshot, limit, value):
    jobs, snapshots, _, _, _, _, _ = setup_snapshot
    setattr(snapshots, limit, value)
    with pytest.raises(ValueError, match="snapshot_.*_limit"):
        capture(setup_snapshot)
    with jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_snapshots").fetchone()[0] == 0
    assert not list(snapshots.root.glob("**/*.json"))


def test_concurrent_capture_is_idempotent_without_orphan_file(setup_snapshot):
    _, snapshots, receipts, context, ref, _, _ = setup_snapshot

    async def concurrent():
        return await asyncio.gather(
            *(snapshots.capture(context, "same", (ref,), receipts=receipts) for _ in range(2))
        )

    a, b = asyncio.run(concurrent())
    assert a["snapshot_id"] == b["snapshot_id"]
    assert len(list((snapshots.root / "snapshots").glob("*.json"))) == 1


def test_summary_receipt_cannot_become_observation_data(setup_snapshot):
    _, snapshots, receipts, context, _, _, _ = setup_snapshot
    payload = {"status": "ok", "representation": "page_summary", "data": {"count": 200}}
    stored = receipts.put(
        context.scope_key, "run_summary", "call-summary", "get_scat_track_points", payload
    )
    ref = SourceReference(
        kind="readonly_result",
        source_id=stored["result_id"],
        run_id="run_summary",
        payload_sha256=stored["payload_sha256"],
    )
    with pytest.raises(SnapshotUnavailable, match="requires_actual_data"):
        asyncio.run(snapshots.capture(context, "summary", (ref,), receipts=receipts))


def test_model_artifact_must_match_owner_thread_run_and_digest(setup_snapshot):
    _, snapshots, receipts, context, _, _, _ = setup_snapshot
    payload = {"blocks": [{"type": "table", "rows": [{"value": 2}]}]}
    record = SimpleNamespace(
        owner_user_id=context.user_id,
        thread_id=context.thread_id,
        run_id="a" * 32,
        payload_sha256=digest(payload),
        expires_at=datetime.fromtimestamp(1100, UTC),
        analysis_result=SimpleNamespace(
            model_dump=lambda **kw: payload, operation="compare_models"
        ),
    )
    repo = SimpleNamespace(get_for_owner=AsyncMock(return_value=record))
    ref = SourceReference(
        kind="analysis_artifact",
        source_id="model-compare-1234",
        run_id=record.run_id,
        payload_sha256=record.payload_sha256,
    )
    result = asyncio.run(
        snapshots.capture(context, "artifact", (ref,), receipts=receipts, artifacts=repo)
    )
    assert result["payload"]["datasets"][0]["payload"] == payload
    repo.get_for_owner.assert_awaited_once_with(ref.source_id, owner_user_id="alice")
    record.thread_id = "another-thread"
    with pytest.raises(SnapshotUnavailable):
        asyncio.run(
            snapshots.capture(context, "wrong-thread", (ref,), receipts=receipts, artifacts=repo)
        )
    record.thread_id, record.run_id = context.thread_id, "b" * 32
    with pytest.raises(JobConflict, match="source_changed"):
        asyncio.run(
            snapshots.capture(context, "wrong-run", (ref,), receipts=receipts, artifacts=repo)
        )


def test_application_admission_reuses_snapshot_and_rechecks_revocation(setup_snapshot):
    jobs, snapshots, receipts, context, ref, _, _ = setup_snapshot
    app = VisualizationTasks(jobs, snapshots, receipts)
    kwargs = {
        "intent": "Draw the fetched range",
        "references": (ref,),
        "preferences_sha256": "a" * 64,
    }
    first = asyncio.run(app.prepare(context, "request-1", **kwargs))
    assert asyncio.run(app.prepare(context, "request-1", **kwargs))["task_id"] == first["task_id"]
    snapshots.revoke(context, first["request"].snapshot_id)
    with pytest.raises(SnapshotUnavailable):
        app.require_snapshot(context, first["task_id"])


def test_factory_respects_disabled_feature_and_maps_operator_limits(setup_snapshot, tmp_path):
    from application.visualization_tasks import create_visualization_tasks
    from visualization_runtime.settings import ConfigurationError, VisualizationSettings

    receipts = setup_snapshot[2]
    with pytest.raises(ConfigurationError, match="disabled"):
        create_visualization_tasks(
            tmp_path, tmp_path / "disabled.sqlite", VisualizationSettings(enabled=False), receipts
        )
    assert not (tmp_path / "disabled.sqlite").exists()
    settings = VisualizationSettings(
        enabled=True,
        code_api_key="test-code",
        review_api_key="test-review",
        code_base_url="https://api.deepseek.com",
        review_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        daily_provider_requests=5,
        max_revisions=1,
        task_timeout_seconds=60,
    )
    app = create_visualization_tasks(tmp_path, tmp_path / "factory.sqlite", settings, receipts)
    assert app.jobs.limits.daily_provider_requests == 5
    assert app.jobs.limits.max_revisions == 1 and app.jobs.limits.wall_seconds == 60
