"""One persisted budget for the approved job and all delegated Hermes sessions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from application.task_store import TaskConflict, canonical
from hermes_runtime.skill_budget import BudgetExhausted, RunBudget
from hermes_runtime.skill_policy import Budget


def ensure_account(jobs, row):
    return RunBudget(
        jobs.path,
        row["scope"],
        row["run_id"],
        Budget.model_validate(jobs.limits(row["task_id"])),
        row["snapshot"]["skills"]["policy_sha256"],
        clock=jobs.clock,
    )


def import_parent(jobs, row, root):
    """Import the finished planner's actual/reserved usage once; children never reset it."""
    account = ensure_account(jobs, row)
    parent = row["snapshot"]["skills"].get("parent_budget")
    source = None
    if parent:
        path = (Path(root) / parent["path"]).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
            return False
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            found = db.execute(
                "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (row["scope"], parent["run_id"]),
            ).fetchone()
        if found is None:
            return False
        source = dict(found)
        # Once finished or deadline-fenced no new planner calls may be admitted.
        if (
            source["finished_at"] is None
            and jobs.clock()
            < source["started_at"] + json.loads(source["limits_json"])["wall_seconds"]
        ):
            return False
    with jobs.transaction() as db:
        flag = db.execute(
            "SELECT parent_imported FROM skill_jobs WHERE task_id=?", (row["task_id"],)
        ).fetchone()
        if flag is None:
            raise TaskConflict("Approved job required")
        if flag[0]:
            return True
        if source:
            fields = [
                "llm_calls",
                "tool_calls",
                "charged_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
                "provider_cached_tokens",
            ]
            db.execute(
                "UPDATE skill_run_budgets SET "
                + ",".join(name + "=" + name + "+?" for name in fields)
                + ",started_at=MIN(started_at,?) WHERE scope=? AND run_id=?",
                (*[source[f] for f in fields], source["started_at"], row["scope"], row["run_id"]),
            )
        db.execute("UPDATE skill_jobs SET parent_imported=1 WHERE task_id=?", (row["task_id"],))
        jobs.event(
            db,
            row["task_id"],
            "parent_budget_imported",
            source_run=parent["run_id"] if parent else None,
        )
    # Enforce imported usage before admitting a single child effect.
    snapshot = account.snapshot()
    for field, limit in [
        ("llm_calls", "max_turns"),
        ("tool_calls", "max_tool_calls"),
        ("charged_tokens", "max_llm_tokens"),
    ]:
        if snapshot[field] >= getattr(account.limits, limit):
            account.exhaust(limit)
    return True


class JobBudget(RunBudget):
    def __init__(self, jobs, row, session):
        self.jobs, self.job_row, self.session = jobs, row, session
        self._admitting = False
        super().__init__(
            jobs.path,
            row["scope"],
            row["run_id"],
            Budget.model_validate(jobs.limits(row["task_id"])),
            row["snapshot"]["skills"]["policy_sha256"],
            clock=jobs.clock,
        )

    def _row(self, db):
        if self._admitting:
            try:
                self.jobs.fence(
                    db,
                    self.session["task_id"],
                    self.session["worker"],
                    self.session["generation"],
                    self.session["revision"],
                )
            except TaskConflict:
                raise BudgetExhausted("job_session_fenced") from None
        row = super()._row(db)
        self.limits = Budget.model_validate_json(row["limits_json"])
        return row

    def admit_tool(self):
        with self.lock:
            self._admitting = True
            try:
                return super().admit_tool()
            finally:
                self._admitting = False

    def reserve_llm(self, kwargs):
        with self.lock:
            self._admitting = True
            try:
                return super().reserve_llm(kwargs)
            finally:
                self._admitting = False

    def exhaust(self, reason):
        with self.lock, self.jobs.transaction() as db:
            try:
                self.jobs.fence(
                    db,
                    self.session["task_id"],
                    self.session["worker"],
                    self.session["generation"],
                    self.session["revision"],
                )
            except TaskConflict:
                return
            self._update(db, exhausted=reason)

    def finish(self):
        # Finishing a delegated native conversation does not finish or reset the job account.
        pass


def revise(jobs, row, limits, ceiling):
    from hermes_runtime.skill_policy import lower_budget

    candidate = lower_budget(
        Budget.model_validate(ceiling), {**jobs.limits(row["task_id"]), **limits}
    )
    if row["job"]["state"] != "paused":
        raise TaskConflict("Budget revision requires a paused job")
    with jobs.transaction() as db:
        current = db.execute(
            "SELECT * FROM skill_jobs WHERE task_id=?", (row["task_id"],)
        ).fetchone()
        if (
            current["state"] != "paused"
            or current["budget_revision"] != row["job"]["budget_revision"]
        ):
            raise TaskConflict("Budget revision changed concurrently")
        usage = db.execute(
            "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
            (row["scope"], row["run_id"]),
        ).fetchone()
        if usage and any(
            usage[used] >= getattr(candidate, cap)
            for used, cap in [
                ("llm_calls", "max_turns"),
                ("tool_calls", "max_tool_calls"),
                ("charged_tokens", "max_llm_tokens"),
            ]
        ):
            raise TaskConflict("Revised budget is already exhausted")
        if usage and jobs.clock() - usage["started_at"] >= candidate.wall_seconds:
            raise TaskConflict("Revised wall budget is already exhausted")
        revision = current["budget_revision"] + 1
        db.execute(
            "INSERT INTO skill_job_budget_revisions VALUES(?,?,?,?)",
            (row["task_id"], revision, canonical(candidate.model_dump()), jobs.clock()),
        )
        db.execute(
            "UPDATE skill_jobs SET budget_revision=?,generation=generation+1 WHERE task_id=?",
            (revision, row["task_id"]),
        )
        db.execute(
            "UPDATE skill_run_budgets SET limits_json=?,exhausted=NULL,finished_at=NULL WHERE scope=? AND run_id=?",
            (json.dumps(candidate.model_dump()), row["scope"], row["run_id"]),
        )
        jobs.event(
            db, row["task_id"], "budget_revised", revision=revision, limits=candidate.model_dump()
        )
    return candidate
