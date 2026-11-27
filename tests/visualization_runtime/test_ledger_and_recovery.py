"""No duplicate calls, no refunded unknowns and no recovery past active resources."""

import asyncio
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from application.context import ExecutionContext
from visualization_runtime.contracts import ExecutionLimits, VisualizationRequest
from visualization_runtime.ledger import BudgetExceeded, ExecutionLedger
from visualization_runtime.ledger_provider import LedgerProvider
from visualization_runtime.providers import Completion, ProviderError
from visualization_runtime.settings import VisualizationSettings
from visualization_runtime.store import JobConflict, VisualizationJobStore


@pytest.fixture
def setup_execution(tmp_path):
    now = [1700000000.0]
    jobs = VisualizationJobStore(
        tmp_path / "tasks.sqlite", workspace_root=tmp_path, clock=lambda: now[0]
    )
    context = ExecutionContext("alice", "thread-1")
    request = VisualizationRequest(
        intent="Plot data",
        snapshot_id="a" * 32,
        snapshot_sha256="b" * 64,
        preferences_sha256="c" * 64,
    )
    jobs.create(context, "request-1", request)
    job = jobs.claim("worker")
    return jobs, ExecutionLedger(jobs), context, request, job, now


def reserve(setup, key="code-1", tokens=100, role="code"):
    _, ledger, _, _, job, _ = setup
    return ledger.reserve_call(
        job["task_id"],
        "worker",
        job["generation"],
        call_key=key,
        role=role,
        fingerprint="a" * 64,
        tokens=tokens,
    )


def finish(setup, row, *, completion=None, **kwargs):
    _, ledger, _, _, job, _ = setup
    ledger.start_call(job["task_id"], "worker", job["generation"], row["call_id"])
    ledger.finish_call(
        job["task_id"], "worker", job["generation"], row["call_id"], completion=completion, **kwargs
    )


def response(tokens=5):
    return Completion(
        "export async function render() {}",
        True,
        {"prompt_tokens": tokens - 2, "completion_tokens": 2, "total_tokens": tokens},
    )


def test_admission_is_atomic_and_same_call_cannot_start_twice(setup_execution):
    row = reserve(setup_execution)
    assert reserve(setup_execution)["call_id"] == row["call_id"]
    _, ledger, _, _, job, _ = setup_execution
    with ThreadPoolExecutor(2) as pool:

        def start(_):
            try:
                ledger.start_call(job["task_id"], "worker", job["generation"], row["call_id"])
                return True
            except JobConflict:
                return False

        assert sum(pool.map(start, range(2))) == 1


def test_complete_usage_releases_unused_reservation_and_replay_is_verified(setup_execution):
    row = reserve(setup_execution)
    finish(setup_execution, row, completion=response())
    saved = reserve(setup_execution)
    assert saved["charged_tokens"] == 5
    assert ExecutionLedger.saved_completion(saved).content == response().content
    with pytest.raises(JobConflict, match="integrity_failed"):
        ExecutionLedger.saved_completion(saved | {"result": "{}"})


def test_unknown_retains_budget_blocks_calls_and_is_not_requeued(setup_execution):
    jobs, ledger, context, _, job, _ = setup_execution
    row = reserve(setup_execution)
    finish(setup_execution, row, error_code="provider_timeout", unknown=True)
    assert jobs.get(context, job["task_id"])["state"] == "needs_reconciliation"
    with pytest.raises(JobConflict, match="lease_lost"):
        reserve(setup_execution, "retry")
    assert (
        ledger.reconcile(job["task_id"], job["generation"], verify_stopped=lambda _: True)
        == "failed"
    )
    with jobs.transaction() as db:
        stored = db.execute("SELECT * FROM visualization_provider_calls").fetchone()
        assert stored["state"] == "unknown" and stored["charged_tokens"] == 100


def test_failed_or_partial_usage_remains_conservatively_charged(setup_execution):
    row = reserve(setup_execution)
    finish(setup_execution, row, error_code="authentication_failed", usage={"prompt_tokens": 0})
    assert reserve(setup_execution)["charged_tokens"] == 100


def test_task_and_daily_budgets_cannot_be_reset_by_another_job(setup_execution):
    jobs, ledger, context, request, job, _ = setup_execution
    jobs.limits = ExecutionLimits(max_provider_requests=1, daily_provider_requests=1)
    row = reserve(setup_execution)
    finish(setup_execution, row, completion=response())
    with pytest.raises(BudgetExceeded, match="task_budget"):
        reserve(setup_execution, "second")
    jobs.create(context, "request-2", request)
    jobs.fail(job["task_id"], "worker", job["generation"], error_code="fixture_finished")
    other = jobs.claim("worker-2")
    with pytest.raises(BudgetExceeded, match="daily_budget"):
        ledger.reserve_call(
            other["task_id"],
            "worker-2",
            other["generation"],
            call_key="new-job",
            role="review",
            fingerprint="b" * 64,
            tokens=100,
        )


def test_usage_over_reservation_is_accounted_and_stops_job(setup_execution):
    jobs, _, context, _, job, _ = setup_execution
    row = reserve(setup_execution, tokens=10)
    finish(setup_execution, row, completion=response(20))
    assert jobs.get(context, job["task_id"])["error_code"] == "provider_reservation_overrun"
    with jobs.transaction() as db:
        assert (
            db.execute("SELECT charged_tokens FROM visualization_provider_calls").fetchone()[0]
            == 20
        )


def test_expiry_distinguishes_never_sent_from_sending(setup_execution):
    jobs, ledger, context, _, job, now = setup_execution
    reserve(setup_execution, "not-sent")
    sent = reserve(setup_execution, "sent")
    ledger.start_call(job["task_id"], "worker", job["generation"], sent["call_id"])
    now[0] += 61
    jobs.get(context, job["task_id"])
    with jobs.transaction() as db:
        rows = {r["call_key"]: r for r in db.execute("SELECT * FROM visualization_provider_calls")}
    assert rows["not-sent"]["state"] == "aborted" and rows["not-sent"]["charged_tokens"] == 0
    assert rows["sent"]["state"] == "unknown" and rows["sent"]["charged_tokens"] == 100


def prepared_render(setup):
    jobs, ledger, _, _, job, _ = setup
    row = reserve(setup)
    finish(setup, row, completion=response())
    attempt = ledger.save_attempt(
        job["task_id"],
        "worker",
        job["generation"],
        step_key="candidate-1",
        code_call_id=row["call_id"],
        source=response().content,
    )
    jobs.transition(
        job["task_id"], "worker", job["generation"], expected_phase="generating", phase="rendering"
    )
    ledger.register_resource(
        job["task_id"],
        "worker",
        job["generation"],
        attempt_id=attempt["attempt_id"],
        resource_id="vis-container-1",
    )
    return attempt


def test_render_recovery_requires_cleanup_and_preserves_phase_and_response(setup_execution):
    jobs, ledger, context, _, job, now = setup_execution
    prepared_render(setup_execution)
    now[0] += 61
    jobs.get(context, job["task_id"])
    with pytest.raises(JobConflict, match="resources_still_active"):
        ledger.reconcile(job["task_id"], job["generation"], verify_stopped=lambda _: False)
    checked = []
    assert (
        ledger.reconcile(
            job["task_id"],
            job["generation"],
            verify_stopped=lambda resource: checked.append(resource) or True,
        )
        == "queued"
    )
    assert checked == ["vis-container-1"]
    resumed = jobs.claim("new-worker")
    assert resumed["phase"] == "rendering" and resumed["generation"] > job["generation"]
    with jobs.transaction() as db:
        assert db.execute("SELECT count(*) FROM visualization_provider_calls").fetchone()[0] == 1


def test_cancelled_task_releases_slot_only_after_verified_cleanup(setup_execution):
    jobs, ledger, context, _, job, _ = setup_execution
    prepared_render(setup_execution)
    cancelled = jobs.cancel(context, job["task_id"])
    with pytest.raises(JobConflict):
        ledger.reconcile(job["task_id"], job["generation"], verify_stopped=lambda _: True)
    assert (
        ledger.reconcile(job["task_id"], cancelled["generation"], verify_stopped=lambda _: True)
        == "cancelled"
    )


def test_recovery_does_not_extend_original_deadline(setup_execution):
    jobs, ledger, context, _, job, now = setup_execution
    now[0] += 901
    jobs.get(context, job["task_id"])
    assert (
        ledger.reconcile(job["task_id"], job["generation"], verify_stopped=lambda _: True)
        == "failed"
    )
    assert jobs.get(context, job["task_id"])["error_code"] == "visualization_deadline_exceeded"


def test_candidates_require_saved_code_and_obey_revision_cap(setup_execution):
    jobs, ledger, _, _, job, _ = setup_execution
    jobs.limits = ExecutionLimits(max_revisions=0)
    row = reserve(setup_execution)
    with pytest.raises(JobConflict, match="verified_code"):
        ledger.save_attempt(
            job["task_id"],
            "worker",
            job["generation"],
            step_key="first",
            code_call_id=row["call_id"],
            source="code",
        )
    finish(setup_execution, row, completion=response())
    first = ledger.save_attempt(
        job["task_id"],
        "worker",
        job["generation"],
        step_key="first",
        code_call_id=row["call_id"],
        source="code",
    )
    assert first["source_sha"] == hashlib.sha256(b"code").hexdigest()
    with pytest.raises(BudgetExceeded, match="revision_budget"):
        ledger.save_attempt(
            job["task_id"],
            "worker",
            job["generation"],
            step_key="second",
            code_call_id=row["call_id"],
            source="more code",
        )


def test_ledger_provider_reuses_saved_response_without_network_repeat(setup_execution):
    _, ledger, _, _, job, _ = setup_execution
    config = VisualizationSettings(
        code_api_key="unit-key", code_base_url="https://api.deepseek.com"
    ).provider("code")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": config.model,
                "choices": [{"finish_reason": "stop", "message": {"content": "code"}}],
                "usage": response().usage,
            },
        )

    provider = LedgerProvider(ledger, config, transport=httpx.MockTransport(respond))
    kwargs = {
        "call_key": "logical-call",
        "messages": [{"role": "user", "content": "synthetic"}],
        "reserved_tokens": 100,
        "max_tokens": 10,
    }
    one = asyncio.run(provider.complete(job["task_id"], "worker", job["generation"], **kwargs))
    two = asyncio.run(provider.complete(job["task_id"], "worker", job["generation"], **kwargs))
    assert one == two and len(calls) == 1
    with pytest.raises(JobConflict, match="key_conflict"):
        asyncio.run(
            provider.complete(
                job["task_id"], "worker", job["generation"], **(kwargs | {"messages": []})
            )
        )


def test_transport_failure_is_saved_as_unknown_without_retry(setup_execution):
    jobs, ledger, context, _, job, _ = setup_execution
    config = VisualizationSettings(
        code_api_key="unit-key", code_base_url="https://api.deepseek.com"
    ).provider("code")
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("private failure", request=request)

    provider = LedgerProvider(ledger, config, transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderError, match="timeout"):
        asyncio.run(
            provider.complete(
                job["task_id"],
                "worker",
                job["generation"],
                call_key="timeout",
                messages=[],
                reserved_tokens=100,
                max_tokens=10,
            )
        )
    assert len(calls) == 1 and jobs.get(context, job["task_id"])["state"] == "needs_reconciliation"


def test_v1_database_upgrade_preserves_job_and_initial_deadline(setup_execution, tmp_path):
    jobs, _, context, _, job, _ = setup_execution
    with sqlite3.connect(jobs.path) as db:
        db.execute("DELETE FROM visualization_schema")
        db.execute("INSERT INTO visualization_schema VALUES(1)")
        db.execute("DROP TABLE visualization_job_budgets")
    reopened = VisualizationJobStore(jobs.path, workspace_root=tmp_path, clock=jobs.clock)
    assert reopened.get(context, job["task_id"])["task_id"] == job["task_id"]
    with reopened.transaction() as db:
        assert (
            db.execute("SELECT deadline FROM visualization_job_budgets").fetchone()[0]
            == job["created"] + 900
        )


def test_failed_render_does_not_release_slot_before_cleanup(setup_execution):
    jobs, ledger, context, _, job, _ = setup_execution
    prepared_render(setup_execution)
    jobs.fail(job["task_id"], "worker", job["generation"], error_code="render_failed")
    assert jobs.get(context, job["task_id"])["state"] == "needs_reconciliation"
    assert (
        ledger.reconcile(job["task_id"], job["generation"], verify_stopped=lambda _: True)
        == "failed"
    )
    assert jobs.get(context, job["task_id"])["error_code"] == "render_failed"


def test_normal_cleanup_verifies_container_before_marking_stopped(setup_execution):
    jobs, ledger, context, _, job, _ = setup_execution
    prepared_render(setup_execution)
    with pytest.raises(JobConflict, match="resources_still_active"):
        ledger.confirm_resource_stopped(
            job["task_id"],
            "worker",
            job["generation"],
            "vis-container-1",
            verify_stopped=lambda _: False,
        )
    ledger.confirm_resource_stopped(
        job["task_id"],
        "worker",
        job["generation"],
        "vis-container-1",
        verify_stopped=lambda _: True,
    )
    jobs.fail(job["task_id"], "worker", job["generation"], error_code="review_rejected")
    assert jobs.get(context, job["task_id"])["state"] == "failed"


def test_queued_job_expires_without_claiming_a_resource(setup_execution):
    jobs, _, context, request, _, now = setup_execution
    queued = jobs.create(context, "expires", request)
    now[0] += 901
    assert jobs.claim("later-worker") is None
    assert jobs.get(context, queued["task_id"])["error_code"] == "visualization_deadline_exceeded"


def test_daily_reservations_are_atomic_across_jobs(setup_execution):
    jobs, ledger, _, request, first, _ = setup_execution
    jobs.limits = ExecutionLimits(daily_provider_requests=1)
    jobs.create(ExecutionContext("alice", "thread-2"), "parallel", request)
    second = jobs.claim("worker-2", concurrency=2, per_owner=2)

    def admit(pair):
        job, worker = pair
        try:
            ledger.reserve_call(
                job["task_id"],
                worker,
                job["generation"],
                call_key="daily",
                role="code",
                fingerprint="a" * 64,
                tokens=100,
            )
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(admit, ((first, "worker"), (second, "worker-2")))) == 1


def test_restart_reuses_saved_code_without_another_request(setup_execution):
    jobs, ledger, context, _, job, now = setup_execution
    row = reserve(setup_execution)
    finish(setup_execution, row, completion=response())
    now[0] += 61
    jobs.get(context, job["task_id"])
    ledger.reconcile(job["task_id"], job["generation"], verify_stopped=lambda _: True)
    resumed = jobs.claim("new-worker")
    saved = ledger.reserve_call(
        job["task_id"],
        "new-worker",
        resumed["generation"],
        call_key="code-1",
        role="code",
        fingerprint="a" * 64,
        tokens=100,
    )
    assert ledger.saved_completion(saved).content == response().content
    with pytest.raises(JobConflict, match="not_sendable"):
        ledger.start_call(job["task_id"], "new-worker", resumed["generation"], saved["call_id"])


def test_old_worker_cannot_save_a_late_response(setup_execution):
    jobs, ledger, context, _, job, _ = setup_execution
    row = reserve(setup_execution)
    ledger.start_call(job["task_id"], "worker", job["generation"], row["call_id"])
    jobs.cancel(context, job["task_id"])
    with pytest.raises(JobConflict, match="lease_lost"):
        ledger.finish_call(
            job["task_id"], "worker", job["generation"], row["call_id"], completion=response()
        )


def test_current_configuration_cannot_expand_a_frozen_job_budget(setup_execution):
    jobs, _, _, _, _, _ = setup_execution
    jobs.limits = ExecutionLimits(max_total_tokens=500000)
    with pytest.raises(BudgetExceeded, match="task_budget"):
        reserve(setup_execution, tokens=60001)


def test_cancel_after_expiry_prevents_requeue(setup_execution):
    jobs, ledger, context, _, job, now = setup_execution
    now[0] += 61
    jobs.get(context, job["task_id"])
    cancelled = jobs.cancel(context, job["task_id"])
    assert cancelled["error_code"] == "user_cancelled"
    assert (
        ledger.reconcile(job["task_id"], cancelled["generation"], verify_stopped=lambda _: True)
        == "cancelled"
    )


def test_reconciliation_checks_generation_again_after_external_cleanup(setup_execution):
    jobs, ledger, context, _, job, now = setup_execution
    prepared_render(setup_execution)
    now[0] += 61
    jobs.get(context, job["task_id"])

    def stopped(resource):
        jobs.cancel(context, job["task_id"])
        return True

    with pytest.raises(JobConflict, match="reconciliation_changed"):
        ledger.reconcile(job["task_id"], job["generation"], verify_stopped=stopped)


def test_provider_timeout_is_clamped_to_remaining_job_deadline(setup_execution):
    jobs, ledger, _, _, job, _ = setup_execution
    jobs.limits = ExecutionLimits(wall_seconds=3)
    config = VisualizationSettings(
        code_api_key="unit-key", code_base_url="https://api.deepseek.com"
    ).provider("code")

    def respond(request):
        assert request.extensions["timeout"]["read"] <= 3
        return httpx.Response(
            200,
            json={
                "model": config.model,
                "choices": [{"finish_reason": "stop", "message": {"content": "code"}}],
                "usage": response().usage,
            },
        )

    provider = LedgerProvider(ledger, config, transport=httpx.MockTransport(respond))
    asyncio.run(
        provider.complete(
            job["task_id"],
            "worker",
            job["generation"],
            call_key="deadline-clamp",
            messages=[],
            reserved_tokens=100,
            max_tokens=10,
        )
    )
