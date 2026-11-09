"""Allowlisted configuration proposals. Web requests never apply or restart services."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

from hermes_runtime.autonomy_config import AutonomyPolicy
from hermes_runtime.profiles import ROLES, metadata, profile_home, validate_settings
from hermes_runtime.skill_policy import SkillPolicy
from hermes_runtime.skills import checked_path, yaml_load

OPTIONAL_GROUPS = {
    "scat-main": ["scat_tasks", "scat_workflows", "scat_skill_scripts"],
    "scat-worker": [],
    "scat-author": [],
}
LEARNING = {
    "enabled",
    "min_successes",
    "min_distinct_inputs",
    "cooldown_hours",
    "max_drafts_per_day",
    "auto_trials",
}
MEMORY = {"context_chars", "max_pending", "inactivity_days"}
LIMITS = {"daily_requests", "daily_units", "lifetime_requests", "lifetime_units"}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def tool_policy(value):
    if not isinstance(value, dict) or set(value) != set(ROLES):
        raise ValueError("invalid_tool_policy")
    for role, groups in value.items():
        if (
            not isinstance(groups, list)
            or any(not isinstance(group, str) for group in groups)
            or len(groups) != len(set(groups))
            or set(groups) - set(OPTIONAL_GROUPS[role])
        ):
            raise ValueError("tool_group_not_optional_for_role")
    return value


class AgentSettingsStore:
    def __init__(self, root, manifest):
        self.root = Path(root).resolve()
        self.manifest = checked_path(self.root, manifest)
        self.targets = json.loads(self.manifest.read_text())
        if set(self.targets) != {
            "profiles",
            "autonomy",
            "proxy",
            "policy",
            "tools",
            "usage",
            "gates",
        } or set(self.targets["profiles"]) != set(ROLES):
            raise ValueError("invalid_settings_manifest")
        self.path = checked_path(self.root, "var/agent-settings/proposals.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, actor TEXT NOT NULL, section TEXT NOT NULL, base TEXT NOT NULL, payload TEXT NOT NULL, sha TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL)"
            )
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def target(self, section):
        if section in ROLES:
            path = (
                profile_home(self.root, section, self.targets["profiles"][section]) / "config.yaml"
            )
            return checked_path(self.root, path.relative_to(self.root).as_posix())
        key = "autonomy" if section in {"memory", "learning"} else section
        if key not in {"autonomy", "proxy", "policy", "tools"}:
            raise ValueError("unknown_settings_section")
        return checked_path(self.root, self.targets[key])

    def read(self, section):
        path = self.target(section)
        if path.stat().st_size > 65536:
            raise ValueError("settings_file_too_large")
        raw = path.read_bytes()
        value = yaml_load(raw.decode())
        if section in ROLES:
            value = {k: value[k] for k in ("model", "agent", "memory")}
            validate_settings(value)
        elif section in {"memory", "learning"}:
            value = AutonomyPolicy.model_validate(value).model_dump()[section]
            value = {
                k: v for k, v in value.items() if k in (MEMORY if section == "memory" else LEARNING)
            }
        elif section == "proxy":
            if set(value) != LIMITS or any(type(v) is not int or v <= 0 for v in value.values()):
                raise ValueError("finite_proxy_limits_required")
        elif section == "policy":
            value = SkillPolicy.model_validate(value).model_dump()["budgets"]
        else:
            tool_policy(value)
        return {
            "section": section,
            "value": value,
            "config_sha256": digest(raw),
            "runtime_observed": False,
        }

    def validate(self, section, changes):
        current = self.read(section)
        original = yaml_load(self.target(section).read_text())
        if section in ROLES:
            if (
                not isinstance(changes, dict)
                or set(changes) - {"model", "temperature", "max_turns"}
                or not changes
            ):
                raise ValueError("invalid_profile_changes")
            value = json.loads(encoded(current["value"]))
            for k, v in changes.items():
                value["model" if k == "model" else "agent"]["default" if k == "model" else k] = v
            # Only models already registered by this deployment are selectable.
            registered = {"deepseek-flash", original["model"]["default"]}
            if value["model"]["default"] not in registered:
                raise ValueError("model_not_registered")
            validate_settings(value)
            for name, v in value.items():
                original.setdefault(name, {}).update(v)
        elif section in {"memory", "learning"}:
            allowed = MEMORY if section == "memory" else LEARNING
            if not isinstance(changes, dict) or not changes or set(changes) - allowed:
                raise ValueError("setting_not_editable")
            original[section].update(changes)
            AutonomyPolicy.model_validate(original)
        elif section == "proxy":
            if not isinstance(changes, dict) or not changes or set(changes) - LIMITS:
                raise ValueError("invalid_budget_changes")
            if any(type(v) is not int or v < 1 or v > original[k] for k, v in changes.items()):
                raise ValueError("budget_can_only_be_reduced")
            original.update(changes)
        elif section == "policy":
            from hermes_runtime.skill_policy import Budget, lower_budget

            if (
                not isinstance(changes, dict)
                or not changes
                or set(changes) - {"readonly", "background"}
            ):
                raise ValueError("invalid_budget_changes")
            for k, v in changes.items():
                original["budgets"][k] = lower_budget(
                    Budget.model_validate(current["value"][k]), v
                ).model_dump()
            SkillPolicy.model_validate(original)
        else:
            original = tool_policy(changes)
        return current, original

    def stage(self, actor, section, changes, expected, request_id):
        identity = str(UUID(request_id))
        sha = digest(
            encoded(
                {
                    "section": section,
                    "changes": changes,
                    "base": expected,
                    "manifest": digest(self.manifest.read_bytes()),
                }
            ).encode()
        )
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM proposals WHERE id=?", (identity,)).fetchone()
            if old:
                if old["actor"] != actor or old["sha"] != sha:
                    raise ValueError("proposal_request_conflict")
                return dict(old)
            if (
                db.execute("SELECT count(*) FROM proposals WHERE actor=?", (actor,)).fetchone()[0]
                >= 1000
            ):
                raise ValueError("proposal_capacity")
            current, _ = self.validate(section, changes)
            if current["config_sha256"] != expected:
                raise ValueError("settings_revision_conflict")
            db.execute(
                "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    actor,
                    section,
                    expected,
                    encoded(changes),
                    sha,
                    "pending_maintenance",
                    time.time(),
                ),
            )
            return dict(db.execute("SELECT * FROM proposals WHERE id=?", (identity,)).fetchone())

    def snapshot(self, actor):
        sections = [
            self.read(s) for s in [*ROLES, "memory", "learning", "proxy", "policy", "tools"]
        ]
        profiles = [
            metadata(self.root, name, base=base) for name, base in self.targets["profiles"].items()
        ]
        # No .env, SOUL or native state bodies are exposed.
        with closing(self.connect()) as db:
            proposals = [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM proposals WHERE actor=? ORDER BY created DESC LIMIT 100",
                    (actor,),
                )
            ]
        from datetime import datetime, timedelta, timezone

        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        usage_path = checked_path(self.root, self.targets["usage"])
        with closing(sqlite3.connect("file:" + usage_path.as_posix() + "?mode=ro", uri=True)) as db:

            def totals(where="", params=()):
                row = db.execute(
                    "SELECT count(*),coalesce(sum(reserved),0),coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0) FROM model_calls"
                    + where,
                    params,
                ).fetchone()
                return dict(
                    zip(("requests", "reserved_units", "input_tokens", "output_tokens"), row)
                )

            usage = {"day": today, "today": totals(" WHERE day=?", (today,)), "lifetime": totals()}
        return {
            "sections": sections,
            "profiles": profiles,
            "optional_groups": OPTIONAL_GROUPS,
            "usage": usage,
            "proposals": proposals,
            "application": "offline_maintenance_required",
            "runtime_observed": False,
        }

    def apply(self, identity, gate):
        import fcntl

        from hermes_runtime.native_profiles import atomic_text, update_settings

        with (self.path.parent / "apply.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gate(self.targets)
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM proposals WHERE id=?", (str(UUID(identity)),)
                ).fetchone()
                if row is None or row["state"] != "pending_maintenance":
                    raise ValueError("proposal_not_pending")
                changes = json.loads(row["payload"])
                expected_proposal = digest(
                    encoded(
                        {
                            "section": row["section"],
                            "changes": changes,
                            "base": row["base"],
                            "manifest": digest(self.manifest.read_bytes()),
                        }
                    ).encode()
                )
                if expected_proposal != row["sha"]:
                    raise ValueError("settings_manifest_changed")
                current, value = self.validate(row["section"], changes)
                if current["config_sha256"] != row["base"]:
                    raise ValueError("settings_revision_conflict")
                path = self.target(row["section"])
                backup = self.path.parent / (row["id"] + ".before.json")
                if backup.exists():
                    raise ValueError("previous_apply_requires_verification")
                atomic_text(backup, path.read_text())
                if row["section"] in ROLES:
                    update_settings(
                        self.root,
                        row["section"],
                        changes,
                        row["base"],
                        self.targets["profiles"][row["section"]],
                    )
                else:
                    atomic_text(path, encoded(value))
                db.execute(
                    "UPDATE proposals SET state='written_restart_required' WHERE id=?", (row["id"],)
                )
                return {"state": "written_restart_required", "runtime_observed": False}
