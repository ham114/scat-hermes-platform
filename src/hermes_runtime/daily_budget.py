"""Atomic daily and lifetime reservations; failed/unknown calls remain charged."""

import hashlib
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone


class DailyBudget:
    def __init__(
        self,
        path,
        *,
        daily_requests=240,
        daily_units=8000000,
        lifetime_requests=None,
        lifetime_units=None,
        campaign=None,
        validation_reserve=None,
        clock=time.time,
    ):
        self.path, self.clock = path, clock
        self.daily_requests, self.daily_units = daily_requests, daily_units
        self.lifetime_requests, self.lifetime_units = lifetime_requests, lifetime_units
        self.campaign = campaign
        self.validation_reserve = validation_reserve
        if min(daily_requests, daily_units) < 1:
            raise ValueError("invalid_daily_budget")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS model_calls(
                id INTEGER PRIMARY KEY, day TEXT NOT NULL, role TEXT NOT NULL,
                created REAL NOT NULL, reserved INTEGER NOT NULL, request_sha TEXT NOT NULL,
                state TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS model_campaigns(
                id TEXT PRIMARY KEY, base_id INTEGER NOT NULL, max_requests INTEGER NOT NULL,
                max_units INTEGER NOT NULL, expires_at REAL NOT NULL)""")
            if campaign is not None:
                if (
                    set(campaign) != {"id", "max_requests", "max_units", "expires_at"}
                    or not isinstance(campaign["id"], str)
                    or not 1 <= len(campaign["id"]) <= 100
                    or any(
                        type(campaign[k]) is not int or campaign[k] < 1
                        for k in ("max_requests", "max_units")
                    )
                    or type(campaign["expires_at"]) not in {int, float}
                    or not 0 < campaign["expires_at"] < float("inf")
                ):
                    raise ValueError("invalid_model_campaign")
                base = db.execute("SELECT coalesce(max(id),0) FROM model_calls").fetchone()[0]
                db.execute(
                    "INSERT OR IGNORE INTO model_campaigns VALUES(?,?,?,?,?)",
                    (
                        campaign["id"],
                        base,
                        campaign["max_requests"],
                        campaign["max_units"],
                        campaign["expires_at"],
                    ),
                )
                row = db.execute(
                    "SELECT max_requests,max_units,expires_at FROM model_campaigns WHERE id=?",
                    (campaign["id"],),
                ).fetchone()
                if row != (campaign["max_requests"], campaign["max_units"], campaign["expires_at"]):
                    raise ValueError("model_campaign_is_immutable")
        path.chmod(0o600)

    def reserve(self, role, raw, maximum):
        if role not in {"memory", "author", "trial"} or not 1 <= maximum <= 8192:
            raise ValueError("invalid_budget_role_or_output")
        units = len(raw) + maximum + 32
        day = datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=8))).date().isoformat()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            n, used = db.execute(
                "SELECT count(*),coalesce(sum(reserved),0) FROM model_calls WHERE day=?", (day,)
            ).fetchone()
            total, consumed = db.execute(
                "SELECT count(*),coalesce(sum(reserved),0) FROM model_calls"
            ).fetchone()
            if self.campaign is not None:
                base, maximum_requests, maximum_units, expires = db.execute(
                    "SELECT base_id,max_requests,max_units,expires_at FROM model_campaigns WHERE id=?",
                    (self.campaign["id"],),
                ).fetchone()
                campaign_count, campaign_units = db.execute(
                    "SELECT count(*),coalesce(sum(reserved),0) FROM model_calls WHERE id>?", (base,)
                ).fetchone()
                if (
                    self.clock() >= expires
                    or campaign_count >= maximum_requests
                    or campaign_units + units > maximum_units
                ):
                    raise ValueError("model_campaign_budget_exhausted")
            if (
                n >= self.daily_requests
                or used + units > self.daily_units
                or self.lifetime_requests is not None
                and total >= self.lifetime_requests
                or self.lifetime_units is not None
                and consumed + units > self.lifetime_units
            ):
                raise ValueError("autonomy_model_budget_exhausted")
            if self.validation_reserve is not None:
                self.validation_reserve(units, role)
            return db.execute(
                "INSERT INTO model_calls(day,role,created,reserved,request_sha,state) "
                "VALUES(?,?,?,?,?,'reserved')",
                (day, role, self.clock(), units, hashlib.sha256(raw).hexdigest()),
            ).lastrowid

    def finish(self, identity, state, usage=None):
        usage = usage or {}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE model_calls SET state=?,input_tokens=?,output_tokens=? "
                "WHERE id=? AND state='reserved'",
                (state, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), identity),
            )
