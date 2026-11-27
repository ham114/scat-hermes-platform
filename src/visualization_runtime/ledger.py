"""Durable provider reservations, candidate code and verified resource recovery."""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from visualization_runtime.contracts import ExecutionLimits
from visualization_runtime.persistence import fence_pending_calls
from visualization_runtime.providers import Completion
from visualization_runtime.store import JobConflict, canonical


class BudgetExceeded(JobConflict):
    pass


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid_visualization_step_key")


def _usage_tokens(usage):
    values = [usage.get(key) for key in ("prompt_tokens", "completion_tokens")]
    if not all(type(v) is int and v >= 0 for v in values):
        return None
    total = sum(values)
    if "total_tokens" in usage and usage["total_tokens"] != total:
        return None
    return total


class ExecutionLedger:
    def __init__(self, jobs):
        self.jobs = jobs

    def _budget(self, db, job):
        row = db.execute(
            "SELECT * FROM visualization_job_budgets WHERE task_id=?", (job["task_id"],)
        ).fetchone()
        if row is None or self.jobs.clock() >= min(
            row["deadline"], job["created"] + self.jobs.limits.wall_seconds
        ):
            raise BudgetExceeded("visualization_deadline_exceeded")
        frozen = ExecutionLimits.model_validate_json(row["limits_json"])
        return ExecutionLimits(
            **{
                key: min(value, getattr(self.jobs.limits, key))
                for key, value in frozen.model_dump().items()
            }
        )

    def reserve_call(self, task_id, worker, generation, *, call_key, role, fingerprint, tokens):
        _key(call_key)
        if role not in {"code", "review"} or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("invalid_provider_admission")
        if type(tokens) is not int or tokens < 1:
            raise ValueError("invalid_provider_token_reservation")
        with self.jobs.transaction() as db:
            job = self.jobs._fence(db, task_id, worker, generation)
            limits = self._budget(db, job)
            old = db.execute(
                "SELECT * FROM visualization_provider_calls WHERE task_id=? AND call_key=?",
                (task_id, call_key),
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint or old["role"] != role:
                    raise JobConflict("provider_call_key_conflict")
                return dict(old)
            count, charged = db.execute(
                "SELECT count(*),COALESCE(sum(charged_tokens),0) "
                "FROM visualization_provider_calls WHERE task_id=? AND state!='aborted'",
                (task_id,),
            ).fetchone()
            if count >= limits.max_provider_requests or charged + tokens > limits.max_total_tokens:
                raise BudgetExceeded("visualization_task_budget_exceeded")
            now = self.jobs.clock()
            day = datetime.fromtimestamp(now, UTC).date().isoformat()
            daily_count, daily_tokens = db.execute(
                "SELECT count(*),COALESCE(sum(c.charged_tokens),0) FROM visualization_provider_calls c "
                "JOIN visualization_jobs j USING(task_id) WHERE j.owner=? AND c.day=? AND c.state!='aborted'",
                (job["owner"], day),
            ).fetchone()
            if (
                daily_count >= limits.daily_provider_requests
                or daily_tokens + tokens > limits.daily_tokens
            ):
                raise BudgetExceeded("visualization_daily_budget_exceeded")
            identity = uuid4().hex
            from visualization_runtime.parent_budget import reserve_request

            reserve_request(db, self.jobs, task_id)
            db.execute(
                "INSERT INTO visualization_provider_calls VALUES(?,?,?,?,?,?,'reserved',?,?,?,?,NULL,NULL,NULL,NULL)",
                (
                    identity,
                    task_id,
                    call_key,
                    generation,
                    role,
                    fingerprint,
                    tokens,
                    tokens,
                    day,
                    now,
                ),
            )
            return dict(
                db.execute(
                    "SELECT * FROM visualization_provider_calls WHERE call_id=?", (identity,)
                ).fetchone()
            )

    def start_call(self, task_id, worker, generation, call_id):
        with self.jobs.transaction() as db:
            job = self.jobs._fence(db, task_id, worker, generation)
            limits = self._budget(db, job)
            changed = db.execute(
                "UPDATE visualization_provider_calls SET state='sending' WHERE call_id=? "
                "AND task_id=? AND generation=? AND state='reserved'",
                (call_id, task_id, generation),
            ).rowcount
            if changed != 1:
                raise JobConflict("provider_call_not_sendable")
            deadline = db.execute(
                "SELECT deadline FROM visualization_job_budgets WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            return min(deadline, job["created"] + limits.wall_seconds) - self.jobs.clock()

    def finish_call(
        self,
        task_id,
        worker,
        generation,
        call_id,
        *,
        completion=None,
        error_code=None,
        unknown=False,
        usage=None,
    ):
        if (completion is None) == (error_code is None):
            raise ValueError("provider_result_or_error_required")
        if error_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code):
            raise ValueError("invalid_provider_error_code")
        body = canonical(asdict(completion)) if completion is not None else None
        if body and len(body.encode()) > 128 * 1024:
            raise ValueError("provider_saved_result_too_large")
        with self.jobs.transaction() as db:
            self.jobs._fence(db, task_id, worker, generation)
            row = db.execute(
                "SELECT * FROM visualization_provider_calls WHERE call_id=? AND task_id=? "
                "AND generation=? AND state='sending'",
                (call_id, task_id, generation),
            ).fetchone()
            if row is None:
                raise JobConflict("provider_call_not_sending")
            actual = _usage_tokens(completion.usage if completion is not None else usage or {})
            charged = (
                actual
                if actual is not None and not unknown
                else max(row["reserved_tokens"], actual or 0)
            )
            state = "succeeded" if completion is not None else "unknown" if unknown else "failed"
            db.execute(
                "UPDATE visualization_provider_calls SET state=?,charged_tokens=?,finished=?,"
                "result=?,result_sha=?,error_code=? WHERE call_id=?",
                (
                    state,
                    charged,
                    self.jobs.clock(),
                    body,
                    hashlib.sha256(body.encode()).hexdigest() if body else None,
                    error_code,
                    call_id,
                ),
            )
            if actual is not None and actual > row["reserved_tokens"]:
                from visualization_runtime.parent_budget import account_overrun

                account_overrun(db, self.jobs, task_id)
                db.execute(
                    "UPDATE visualization_jobs SET state='needs_reconciliation',"
                    "error_code='provider_reservation_overrun',updated=? WHERE task_id=?",
                    (self.jobs.clock(), task_id),
                )
                fence_pending_calls(db, task_id, self.jobs.clock())
            elif unknown:
                db.execute(
                    "UPDATE visualization_jobs SET state='needs_reconciliation',"
                    "error_code='provider_outcome_unknown',updated=? WHERE task_id=?",
                    (self.jobs.clock(), task_id),
                )
                fence_pending_calls(db, task_id, self.jobs.clock())

    @staticmethod
    def saved_completion(row):
        if row["state"] != "succeeded" or not row["result"]:
            raise JobConflict("provider_result_unavailable")
        raw = row["result"]
        if hashlib.sha256(raw.encode()).hexdigest() != row["result_sha"]:
            raise JobConflict("provider_result_integrity_failed")
        return Completion(**json.loads(raw))

    def save_attempt(
        self, task_id, worker, generation, *, step_key, code_call_id, source, activate=False
    ):
        _key(step_key)
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > 128 * 1024:
            raise ValueError("invalid_visualization_source")
        sha = hashlib.sha256(source.encode()).hexdigest()
        with self.jobs.transaction() as db:
            job = self.jobs._fence(db, task_id, worker, generation)
            limits = self._budget(db, job)
            old = db.execute(
                "SELECT * FROM visualization_attempts WHERE task_id=? AND step_key=?",
                (task_id, step_key),
            ).fetchone()
            if old:
                if old["source_sha"] != sha or old["code_call_id"] != code_call_id:
                    raise JobConflict("visualization_attempt_conflict")
                if activate:
                    if job["phase"] not in {"generating", "revising"}:
                        raise JobConflict("visualization_attempt_phase")
                    db.execute(
                        "UPDATE visualization_jobs SET phase='rendering',updated=? WHERE task_id=?",
                        (self.jobs.clock(), task_id),
                    )
                return dict(old)
            if job["phase"] not in {"generating", "revising"}:
                raise JobConflict("visualization_attempt_phase")
            call = db.execute(
                "SELECT * FROM visualization_provider_calls WHERE call_id=? AND task_id=? "
                "AND role='code' AND state='succeeded'",
                (code_call_id, task_id),
            ).fetchone()
            if call is None:
                raise JobConflict("verified_code_response_required")
            self.saved_completion(call)
            ordinal = db.execute(
                "SELECT count(*) FROM visualization_attempts WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            if ordinal > limits.max_revisions:
                raise BudgetExceeded("visualization_revision_budget_exceeded")
            identity = uuid4().hex
            db.execute(
                "INSERT INTO visualization_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    task_id,
                    step_key,
                    ordinal,
                    generation,
                    code_call_id,
                    source,
                    sha,
                    self.jobs.clock(),
                ),
            )
            if activate:
                db.execute(
                    "UPDATE visualization_jobs SET phase='rendering',updated=? WHERE task_id=?",
                    (self.jobs.clock(), task_id),
                )
            return dict(
                db.execute(
                    "SELECT * FROM visualization_attempts WHERE attempt_id=?", (identity,)
                ).fetchone()
            )

    def register_resource(self, task_id, worker, generation, *, attempt_id, resource_id):
        _key(resource_id)
        with self.jobs.transaction() as db:
            job = self.jobs._fence(db, task_id, worker, generation)
            if job["phase"] != "rendering":
                raise JobConflict("visualization_resource_phase")
            self._budget(db, job)
            attempt = db.execute(
                "SELECT * FROM visualization_attempts WHERE attempt_id=? AND task_id=?",
                (attempt_id, task_id),
            ).fetchone()
            if attempt is None:
                raise JobConflict("visualization_attempt_not_found")
            if hashlib.sha256(attempt["source"].encode()).hexdigest() != attempt["source_sha"]:
                raise JobConflict("visualization_source_integrity_failed")
            old = db.execute(
                "SELECT * FROM visualization_resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if old:
                if (old["task_id"], old["attempt_id"], old["generation"], old["state"]) != (
                    task_id,
                    attempt_id,
                    generation,
                    "active",
                ):
                    raise JobConflict("visualization_resource_conflict")
                return
            db.execute(
                "INSERT INTO visualization_resources VALUES(?,?,?,?,'active',?)",
                (resource_id, task_id, attempt_id, generation, self.jobs.clock()),
            )

    def confirm_resource_stopped(self, task_id, worker, generation, resource_id, *, verify_stopped):
        with self.jobs.transaction() as db:
            self.jobs._fence(db, task_id, worker, generation)
            if (
                db.execute(
                    "SELECT 1 FROM visualization_resources WHERE task_id=? AND resource_id=? "
                    "AND generation=?",
                    (task_id, resource_id, generation),
                ).fetchone()
                is None
            ):
                raise JobConflict("visualization_resource_not_found")
        if verify_stopped(resource_id) is not True:
            raise JobConflict("visualization_resources_still_active")
        with self.jobs.transaction() as db:
            self.jobs._fence(db, task_id, worker, generation)
            db.execute(
                "UPDATE visualization_resources SET state='stopped',updated=? "
                "WHERE task_id=? AND resource_id=? AND generation=?",
                (self.jobs.clock(), task_id, resource_id, generation),
            )

    def reconcile(self, task_id, expected_generation, *, verify_stopped):
        """Trusted launcher callback, never a caller-supplied cleanup boolean."""
        with self.jobs.transaction() as db:
            self.jobs._recover(db)
            job = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                job is None
                or job["state"] != "needs_reconciliation"
                or job["generation"] != expected_generation
            ):
                raise JobConflict("visualization_reconciliation_changed")
            resources = [
                r[0]
                for r in db.execute(
                    "SELECT resource_id FROM visualization_resources "
                    "WHERE task_id=? AND state='active'",
                    (task_id,),
                )
            ]
        # Never hold the SQLite write lock while talking to a container runtime.
        if any(verify_stopped(resource) is not True for resource in resources):
            raise JobConflict("visualization_resources_still_active")
        with self.jobs.transaction() as db:
            current = db.execute(
                "SELECT * FROM visualization_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                current["state"] != "needs_reconciliation"
                or current["generation"] != expected_generation
            ):
                raise JobConflict("visualization_reconciliation_changed")
            active = {
                r[0]
                for r in db.execute(
                    "SELECT resource_id FROM visualization_resources "
                    "WHERE task_id=? AND state='active'",
                    (task_id,),
                )
            }
            if active != set(resources):
                raise JobConflict("visualization_resources_changed")
            fence_pending_calls(db, task_id, self.jobs.clock())
            db.execute(
                "UPDATE visualization_resources SET state='stopped',updated=? WHERE task_id=?",
                (self.jobs.clock(), task_id),
            )
            unknown = db.execute(
                "SELECT 1 FROM visualization_provider_calls WHERE task_id=? AND state='unknown'",
                (task_id,),
            ).fetchone()
            state, error = "queued", None
            if current["error_code"] == "user_cancelled":
                state, error = "cancelled", "user_cancelled"
            elif unknown:
                state, error = "failed", "provider_outcome_unknown"
            elif current["error_code"] == "provider_reservation_overrun":
                state, error = "failed", "provider_reservation_overrun"
            elif current["resume_phase"] == "failed":
                state, error = "failed", current["error_code"]
            elif current["phase"] == "publishing":
                raise JobConflict("visualization_publication_requires_verification")
            else:
                try:
                    self._budget(db, current)
                except BudgetExceeded:
                    state, error = "failed", "visualization_deadline_exceeded"
            db.execute(
                "UPDATE visualization_jobs SET state=?,resume_phase=?,worker=NULL,lease_until=NULL,"
                "generation=generation+1,error_code=?,updated=? WHERE task_id=?",
                (
                    state,
                    current["phase"] if state == "queued" else None,
                    error,
                    self.jobs.clock(),
                    task_id,
                ),
            )
            self.jobs._event(db, current, "reconciled")
            return state
