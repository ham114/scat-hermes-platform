"""Immutable, scope-selected aggregate validation caps across native processes.

Reservations are never refunded, including failed or unknown upstream outcomes.
Ordinary user scopes are unaffected. Existing per-run limits still apply.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CampaignExhausted(RuntimeError):
    pass


class ValidationCampaign:
    def __init__(self, root, policy, *, clock=time.time):
        self.root, self.policy, self.clock = Path(root).resolve(), policy, clock
        if (
            set(policy)
            != {"schema_version", "id", "scopes", "max_calls", "max_units", "expires_at"}
            or type(policy["schema_version"]) is not int
            or policy["schema_version"] != 1
            or not isinstance(policy["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", policy["id"])
            or not isinstance(policy["scopes"], list)
            or not 1 <= len(policy["scopes"]) <= 100
            or len(policy["scopes"]) != len(set(policy["scopes"]))
            or any(
                not isinstance(s, str) or not re.fullmatch(r"scat:[0-9a-f]{64}", s)
                for s in policy["scopes"]
            )
            or type(policy["max_calls"]) is not int
            or not 1 <= policy["max_calls"] <= 240
            or type(policy["max_units"]) is not int
            or not 1 <= policy["max_units"] <= 8000000
            or type(policy["expires_at"]) not in {int, float}
            or not math.isfinite(policy["expires_at"])
        ):
            raise ValueError("invalid_validation_campaign")
        self.sha = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.path = self.root / "var/autonomy/validation/state.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS validation_campaigns(id TEXT PRIMARY KEY,sha TEXT NOT NULL,definition TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS validation_calls(id INTEGER PRIMARY KEY,campaign TEXT NOT NULL,scope TEXT NOT NULL,run TEXT NOT NULL,units INTEGER NOT NULL,created REAL NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO validation_campaigns VALUES(?,?,?)",
                (policy["id"], self.sha, json.dumps(policy, sort_keys=True)),
            )
            if (
                db.execute(
                    "SELECT sha FROM validation_campaigns WHERE id=?", (policy["id"],)
                ).fetchone()[0]
                != self.sha
            ):
                raise ValueError("validation_campaign_is_immutable")
        self.path.chmod(0o600)

    @classmethod
    def for_scope(cls, scope):
        filename = os.environ.get("SCAT_VALIDATION_CAMPAIGN_FILE")
        if not filename:
            return None
        original = ROOT / filename
        path = original.resolve()
        if not path.is_relative_to(ROOT.resolve()) or original.is_symlink():
            raise ValueError("validation_campaign_path")
        policy = json.loads(path.read_text())
        if policy == {"schema_version": 1, "status": "retired"}:
            return None
        campaign = cls(ROOT, policy)
        if scope not in policy.get("scopes", []):
            return None
        return campaign

    def reserve(self, scope, run, units):
        if scope not in self.policy["scopes"] or type(units) is not int or units < 1:
            raise ValueError("invalid_validation_reservation")
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            calls, total = db.execute(
                "SELECT count(*),coalesce(sum(units),0) FROM validation_calls WHERE campaign=?",
                (self.policy["id"],),
            ).fetchone()
            if (
                self.clock() >= self.policy["expires_at"]
                or calls >= self.policy["max_calls"]
                or total + units > self.policy["max_units"]
            ):
                raise CampaignExhausted("validation_campaign_exhausted")
            db.execute(
                "INSERT INTO validation_calls(campaign,scope,run,units,created) VALUES(?,?,?,?,?)",
                (self.policy["id"], scope, run, units, self.clock()),
            )
