"""Persistent per-run readonly admission and conservative LLM reservations."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from threading import RLock

from hermes_runtime.skill_policy import Budget


class BudgetExhausted(RuntimeError):
    pass


class RunBudget:
    def __init__(self, path, scope, run_id, limits: Budget, policy_sha256, *, clock=time.time):
        self.path, self.scope, self.run_id = path, scope, run_id
        self.limits, self.clock = limits, clock
        self.lock = RLock()
        from hermes_runtime.validation_campaign import ValidationCampaign

        self.campaign = ValidationCampaign.for_scope(scope)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS skill_run_budgets (
                scope TEXT NOT NULL, run_id TEXT NOT NULL, started_at REAL NOT NULL,
                policy_sha256 TEXT NOT NULL, limits_json TEXT NOT NULL,
                llm_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
                charged_tokens INTEGER NOT NULL DEFAULT 0,
                provider_input_tokens INTEGER NOT NULL DEFAULT 0,
                provider_output_tokens INTEGER NOT NULL DEFAULT 0,
                provider_cached_tokens INTEGER NOT NULL DEFAULT 0,
                exhausted TEXT, finished_at REAL, PRIMARY KEY(scope,run_id))""")
            db.execute(
                "INSERT OR IGNORE INTO skill_run_budgets "
                "(scope,run_id,started_at,policy_sha256,limits_json) VALUES(?,?,?,?,?)",
                (scope, run_id, clock(), policy_sha256, json.dumps(limits.model_dump())),
            )
            row = db.execute(
                "SELECT policy_sha256,limits_json FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (scope, run_id),
            ).fetchone()
            if row != (policy_sha256, json.dumps(limits.model_dump())):
                raise ValueError("run_budget_snapshot_mismatch")

    def _row(self, db):
        db.row_factory = sqlite3.Row
        return dict(
            db.execute(
                "SELECT * FROM skill_run_budgets WHERE scope=? AND run_id=?",
                (self.scope, self.run_id),
            ).fetchone()
        )

    def _update(self, db, **fields):
        db.execute(
            "UPDATE skill_run_budgets SET "
            + ",".join(name + "=?" for name in fields)
            + " WHERE scope=? AND run_id=?",
            (*fields.values(), self.scope, self.run_id),
        )

    def _check(self, row):
        if row["exhausted"]:
            return row["exhausted"]
        if row["finished_at"] is not None:
            return "run_finished"
        if self.clock() - row["started_at"] >= self.limits.wall_seconds:
            return "wall_seconds"
        return None

    def admit_tool(self):
        reason = None
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db)
            reason = self._check(row)
            if not reason and row["tool_calls"] >= self.limits.max_tool_calls:
                reason = "max_tool_calls"
            if reason:
                self._update(db, exhausted=reason)
            else:
                self._update(db, tool_calls=row["tool_calls"] + 1)
        if reason:
            raise BudgetExhausted("budget_exhausted:" + reason)

    def reserve_llm(self, kwargs):
        # Byte reservation intentionally overestimates ordinary DeepSeek tokenization.
        # It is reported separately from provider usage and is not a billing claim.
        estimate = len(json.dumps(kwargs, ensure_ascii=False, default=str).encode("utf-8"))
        reason = None
        cap = 0
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db)
            reason = self._check(row)
            if not reason and row["llm_calls"] >= self.limits.max_turns:
                reason = "max_turns"
            remaining = self.limits.max_llm_tokens - row["charged_tokens"] - estimate
            if not reason and remaining < 1:
                reason = "max_llm_tokens"
            if reason:
                self._update(db, exhausted=reason)
            else:
                requested = (
                    min(
                        int(kwargs.get(key) or 4096)
                        for key in ["max_tokens", "max_completion_tokens", "max_output_tokens"]
                        if key in kwargs
                    )
                    if any(
                        key in kwargs
                        for key in ["max_tokens", "max_completion_tokens", "max_output_tokens"]
                    )
                    else 4096
                )
                cap = min(requested, remaining)
                if cap < 1:
                    raise ValueError("invalid_provider_output_cap")
                from hermes_runtime.validation_campaign import CampaignExhausted

                try:
                    if self.campaign is not None:
                        self.campaign.reserve(self.scope, self.run_id, estimate + cap)
                except CampaignExhausted:
                    reason = "validation_campaign_exhausted"
                    self._update(db, exhausted=reason)
                else:
                    self._update(
                        db,
                        llm_calls=row["llm_calls"] + 1,
                        charged_tokens=row["charged_tokens"] + estimate + cap,
                    )
        if reason:
            raise BudgetExhausted("budget_exhausted:" + reason)
        return (
            estimate,
            cap,
            max(0.01, self.limits.wall_seconds - (self.clock() - row["started_at"])),
        )

    def settle_llm(self, reservation, usage):
        if usage is None:
            return  # Failed/missing usage retains the complete reservation.
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            return
        input_tokens = max(0, int(usage.get("prompt_tokens") or 0))
        output_tokens = max(0, int(usage.get("completion_tokens") or 0))
        if not input_tokens:  # Partial/malformed usage must not refund the reservation.
            return
        details = usage.get("prompt_tokens_details") or {}
        cached = max(
            0, int(usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0)
        )
        estimate, cap, _ = reservation
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db)
            charged = row["charged_tokens"] - cap + output_tokens + max(0, input_tokens - estimate)
            self._update(
                db,
                charged_tokens=charged,
                provider_input_tokens=row["provider_input_tokens"] + input_tokens,
                provider_output_tokens=row["provider_output_tokens"] + output_tokens,
                provider_cached_tokens=row["provider_cached_tokens"] + cached,
                exhausted="max_llm_tokens"
                if charged > self.limits.max_llm_tokens
                else row["exhausted"],
            )

    def wrap_create(self, create):
        def invoke(**kwargs):
            reservation = self.reserve_llm(kwargs)
            _, cap, timeout = reservation
            output_key = next(
                (
                    key
                    for key in ["max_tokens", "max_completion_tokens", "max_output_tokens"]
                    if key in kwargs
                ),
                "max_tokens",
            )
            kwargs[output_key] = cap
            configured_timeout = kwargs.get("timeout")
            if not isinstance(configured_timeout, (int, float)):
                configured_timeout = getattr(configured_timeout, "read", None)
            kwargs["timeout"] = min(float(configured_timeout or timeout), timeout)
            if kwargs.get("stream"):
                kwargs["stream_options"] = {
                    **(kwargs.get("stream_options") or {}),
                    "include_usage": True,
                }
            result = create(**kwargs)
            if kwargs.get("stream"):
                return _BudgetStream(result, self, reservation)
            self.settle_llm(reservation, getattr(result, "usage", None))
            return result

        return invoke

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            row = self._row(db)
        return {
            key: row[key]
            for key in [
                "policy_sha256",
                "limits_json",
                "llm_calls",
                "tool_calls",
                "charged_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
                "provider_cached_tokens",
                "exhausted",
                "finished_at",
                "started_at",
            ]
        }

    def finish(self):
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            self._update(db, finished_at=self.clock())

    def exhaust(self, reason):
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            self._update(db, exhausted=reason)


class _BudgetStream:
    def __init__(self, stream, budget, reservation):
        self.stream, self.budget, self.reservation = stream, budget, reservation
        self.usage, self.settled = None, False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __iter__(self):
        try:
            for chunk in self.stream:
                self.usage = getattr(chunk, "usage", None) or self.usage
                yield chunk
        finally:
            self.close()

    def close(self):
        if not self.settled:
            self.settled = True
            self.budget.settle_llm(self.reservation, self.usage)
            if hasattr(self.stream, "close"):
                self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
