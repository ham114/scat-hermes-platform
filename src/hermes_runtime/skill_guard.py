"""Run-bound skill admission at the pinned native registry dispatch boundary.

This is admission/audit for readonly business operations and proposal preparation,
not the S4 effect-step executor or a replacement for backend authorization.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

from hermes_runtime.skills import SKILL_TOOLS, SkillPolicyError, digest
from workflow_runtime.contracts import PRIMARY_SKILLS


class SkillOperationGuard:
    def __init__(self, bridge, operations, controls):
        self.bridge = bridge
        self.operations = frozenset(operations)
        self.controls = frozenset(controls) | SKILL_TOOLS
        if self.operations & self.controls:
            raise ValueError("Skill business and control operations overlap")
        self._registry = None
        self._dispatch = None
        with closing(sqlite3.connect(bridge.trace_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS skill_operations (
                id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
                scope TEXT NOT NULL, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
                operation TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
                skill_id TEXT NOT NULL, skill_version TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL, file_sha256 TEXT NOT NULL,
                load_id INTEGER NOT NULL, outcome TEXT NOT NULL, result_sha256 TEXT)""")
            db.execute("""CREATE INDEX IF NOT EXISTS skill_operation_run
                ON skill_operations(scope,run_id)""")

    @staticmethod
    def rejected(code):
        return json.dumps({"success": False, "status": "forbidden", "error": code})

    def load_required(self, code, name, session_id, args):
        """Give actionable recovery from the pinned catalog, never grant a load."""
        if code not in {"skill_required", "skill_operation_not_allowed"}:
            return self.rejected(code)
        bridge = self.bridge.session_bridge(session_id)
        if session_id not in bridge._sessions or name not in self.operations:
            return self.rejected(code)
        bridge._verify()
        candidates = [
            {"name": skill, "version": entry["version"], "file_path": "SKILL.md"}
            for skill, entry in bridge.release.entries.items()
            if name in entry["allowed_operations"]
        ]
        if name == "scat_workflow_prepare":
            primary = PRIMARY_SKILLS.get(args.get("proposal", {}).get("kind"))
            candidates = [item for item in candidates if item["name"] == primary]
        return json.dumps(
            {
                "success": False,
                "status": "forbidden",
                "error": code,
                "recovery": {
                    "action": "skill_view",
                    "skills": candidates,
                    "message": "本次调用未执行。请在当前运行显式加载适用技能正文，成功后重试原只读查询或方案准备；上一轮加载不授权本轮。",
                },
            },
            ensure_ascii=False,
        )

    def _admit(self, name, args, session_id):
        bridge = self.bridge.session_bridge(session_id)
        trusted = bridge._sessions.get(session_id)
        if trusted is None:
            raise SkillPolicyError("trusted_skill_context_required")
        if not isinstance(args, dict):
            raise SkillPolicyError("invalid_skill_operation_arguments")
        bridge._verify()
        argument_hash = digest(
            json.dumps(
                args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        )
        with closing(sqlite3.connect(bridge.trace_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT id,skill_id,version,bundle_sha256,file_sha256 FROM skill_loads
                WHERE scope=? AND run_id=? AND session_id=? AND file_path='SKILL.md'
                ORDER BY id DESC""",
                (*trusted, session_id),
            ).fetchall()
            if not rows:
                raise SkillPolicyError("skill_required")
            for load_id, skill_id, version, bundle, file_hash in rows:
                entry = bridge.release.entries.get(skill_id)
                if (
                    entry is None
                    or version != entry["version"]
                    or bundle != bridge.release.bundle_sha256
                    or file_hash != digest(bridge.release.files[f"{skill_id}/SKILL.md"])
                ):
                    raise SkillPolicyError("skill_snapshot_mismatch")
                if name not in entry["allowed_operations"]:
                    continue
                if name == "scat_workflow_prepare":
                    primary = PRIMARY_SKILLS.get(args.get("proposal", {}).get("kind"))
                    if skill_id != primary:
                        continue
                try:
                    bridge.lifecycle.reserve(db, policy=bridge.policy)
                except SkillPolicyError as exc:
                    if str(exc) == "skill_trace_capacity":
                        raise SkillPolicyError("skill_operation_trace_capacity") from None
                    raise
                cursor = db.execute(
                    """INSERT INTO skill_operations
                    (started_at,scope,run_id,session_id,operation,arguments_sha256,
                     skill_id,skill_version,bundle_sha256,file_sha256,load_id,outcome)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        time.time(),
                        *trusted,
                        session_id,
                        name,
                        argument_hash,
                        skill_id,
                        version,
                        bundle,
                        file_hash,
                        load_id,
                        "admitted",
                    ),
                )
                return cursor.lastrowid
        raise SkillPolicyError("skill_operation_not_allowed")

    def _finish(self, call_id, outcome, result=None):
        with closing(sqlite3.connect(self.bridge.trace_path)) as db, db:
            encoded = json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
            changed = db.execute(
                """UPDATE skill_operations SET finished_at=?,outcome=?,result_sha256=?
                WHERE id=? AND outcome='admitted'""",
                (time.time(), outcome, digest(encoded), call_id),
            ).rowcount
            if changed != 1:
                raise SkillPolicyError("skill_operation_receipt_unavailable")

    def invoke(self, dispatch, name, args, *, session_id="", **kwargs):
        from hermes_runtime.skill_budget import BudgetExhausted

        budget = self.bridge._budgets.get(session_id)
        if budget is not None:
            try:
                budget.admit_tool()
            except BudgetExhausted as exc:
                return self.rejected(str(exc))
        if name in self.controls:
            return dispatch(name, args, session_id=session_id, **kwargs)
        if name not in self.operations:
            return self.rejected("skill_operation_not_allowed")
        try:
            # Admission and rebind are serialized. Backend execution retains its captured
            # load identity; a new run cannot inherit an old run's load receipt.
            with self.bridge._lock:
                call_id = self._admit(name, args, session_id)
        except SkillPolicyError as exc:
            try:
                with self.bridge._lock:
                    return self.load_required(str(exc), name, session_id, args)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                return self.rejected("skill_operation_admission_unavailable")
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return self.rejected("skill_operation_admission_unavailable")
        try:
            result = dispatch(name, args, session_id=session_id, **kwargs)
        except Exception:  # noqa: BLE001 -- no backend error text in the model result
            try:
                self._finish(call_id, "dispatch_error")
            except (OSError, ValueError, TypeError, sqlite3.Error, SkillPolicyError):
                pass
            return self.rejected("skill_operation_dispatch_unavailable")
        try:
            # "returned" only means the handler returned; business success remains
            # determined by the actual tool result and trusted result receipt.
            self._finish(call_id, "returned", result)
        except (OSError, ValueError, TypeError, sqlite3.Error, SkillPolicyError):
            return self.rejected("skill_operation_receipt_unavailable")
        return result

    def install(self, registry):
        if self._registry is not None or getattr(registry, "_scat_skill_guard", None) is not None:
            raise RuntimeError("Skill operation guard already installed")
        original = registry.dispatch

        def guarded(name, args, **kwargs):
            return self.invoke(original, name, args, **kwargs)

        registry.dispatch = guarded
        registry._scat_skill_guard = self
        self._registry, self._dispatch = registry, guarded

    def verify_installed(self):
        if self._registry is None or self._registry.dispatch is not self._dispatch:
            raise RuntimeError("Skill operation guard is unavailable")
