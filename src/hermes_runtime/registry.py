"""Owner/run bindings and a separate owner-scoped conversation journal."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from application.context import ExecutionContext
from hermes_runtime.conversation_history import ConversationHistory

_RUN_ID = re.compile(r"run_[A-Za-z0-9_-]{1,128}\Z")


class RunNotFoundError(LookupError):
    pass


class RunBindingConflictError(RuntimeError):
    pass


class ThreadNotFoundError(LookupError):
    pass


class ThreadBusyError(RuntimeError):
    pass


class RequestConflictError(RuntimeError):
    pass


class RunRegistry(ConversationHistory):
    def __init__(
        self, path: Path, *, workspace_root: Path, max_active_per_owner=None, max_active_total=None
    ):
        root = workspace_root.resolve()
        self.path = path.resolve()
        for limit in (max_active_per_owner, max_active_total):
            if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
                raise ValueError("Invalid runtime admission quota")
        self.max_active_per_owner, self.max_active_total = max_active_per_owner, max_active_total
        if self.path == root or not self.path.is_relative_to(root):
            raise ValueError("Run registry must remain inside its workspace")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_runs (
                run_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                request_id TEXT NOT NULL,
                UNIQUE(scope_key, request_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_threads (
                thread_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created_at REAL NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_admissions (
                scope_key TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, run_id TEXT
            )""")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_admission_owners(scope_key TEXT PRIMARY KEY,owner_id TEXT NOT NULL)"
            )
            self.initialize_history(conn)
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def bind(self, context: ExecutionContext, request_id: str, run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise RunBindingConflictError("Invalid upstream run identifier")
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT run_id FROM runtime_runs WHERE scope_key=? AND request_id=?",
                (context.scope_key, request_id),
            ).fetchone()
            if row:
                if row[0] != run_id:
                    raise RunBindingConflictError("Idempotency binding changed")
                self.bind_history(conn, context, request_id, run_id)
                conn.execute(
                    "UPDATE runtime_admissions SET run_id=? WHERE scope_key=? AND request_id=?",
                    (run_id, context.scope_key, request_id),
                )
                return
            try:
                conn.execute(
                    "INSERT INTO runtime_runs VALUES (?, ?, ?)",
                    (run_id, context.scope_key, request_id),
                )
                self.bind_history(conn, context, request_id, run_id)
                conn.execute(
                    "UPDATE runtime_admissions SET run_id=? WHERE scope_key=? AND request_id=?",
                    (run_id, context.scope_key, request_id),
                )
            except sqlite3.IntegrityError:
                raise RunBindingConflictError("Run already bound to another request") from None

    def require(self, context: ExecutionContext, run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise RunNotFoundError("Run not found")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM runtime_runs WHERE run_id=? AND scope_key=?",
                (run_id, context.scope_key),
            ).fetchone()
        if row is None:
            raise RunNotFoundError("Run not found")

    def create_thread(self, owner_id: str) -> dict:
        thread_id = str(uuid4())
        ExecutionContext(owner_id, thread_id)
        record = {"thread_id": thread_id, "created_at": time.time()}
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO runtime_threads VALUES (?,?,?)",
                (thread_id, owner_id, record["created_at"]),
            )
        return record

    def require_thread(self, owner_id: str, thread_id: str) -> ExecutionContext:
        context = ExecutionContext(owner_id, thread_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM runtime_threads WHERE thread_id=? AND owner_id=?",
                (thread_id, owner_id),
            ).fetchone()
        if row is None:
            raise ThreadNotFoundError("Thread not found")
        return context

    def require_scope(self, owner_id: str, scope_key: str) -> ExecutionContext:
        """Resolve a server-supplied native scope inside the configured owner's threads."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT thread_id FROM runtime_threads WHERE owner_id=?", (owner_id,)
            ).fetchall()
        for row in rows:
            context = ExecutionContext(owner_id, row[0])
            if context.scope_key == scope_key:
                return context
        raise ThreadNotFoundError("Thread scope not found")

    def list_threads(self, owner_id: str, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid page size")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT thread_id,created_at FROM runtime_threads WHERE owner_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [{"thread_id": row[0], "created_at": row[1]} for row in rows]

    def require_control_scope(self, scope_key: str, run_id: str) -> ExecutionContext:
        """Resolve a private bridge's native binding through the trusted admission owner."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT o.owner_id FROM runtime_admission_owners o JOIN runtime_runs r "
                "ON r.scope_key=o.scope_key WHERE o.scope_key=? AND r.run_id=?",
                (scope_key, run_id),
            ).fetchone()
        if row is None:
            raise ThreadNotFoundError("Bound control scope not found")
        return self.require_scope(row[0], scope_key)

    def list_runs(self, context: ExecutionContext, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid page size")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT run_id,request_id FROM runtime_runs WHERE scope_key=? "
                "ORDER BY rowid DESC LIMIT ?",
                (context.scope_key, limit),
            ).fetchall()
        return [{"run_id": row[0], "request_id": row[1]} for row in rows]

    def reserve_request(
        self, context: ExecutionContext, request_id: str, payload_sha256: str, message=None
    ):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if message is not None:
                if not isinstance(message, str) or not message.strip() or len(message) > 32000:
                    raise ValueError("Invalid message")
                if __import__("hashlib").sha256(message.encode()).hexdigest() != payload_sha256:
                    raise ValueError("Message digest mismatch")
                # Inside the admission transaction: conflicts roll back both records.
                self.record_question(conn, context, request_id, message, payload_sha256)
            known = conn.execute(
                "SELECT 1 FROM runtime_runs WHERE scope_key=? AND request_id=?",
                (context.scope_key, request_id),
            ).fetchone()
            if known:
                return
            row = conn.execute(
                "SELECT request_id,payload_sha256 FROM runtime_admissions WHERE scope_key=?",
                (context.scope_key,),
            ).fetchone()
            if row:
                if row[0] != request_id:
                    raise ThreadBusyError("Thread has an active or unresolved request")
                if row[1] != payload_sha256:
                    raise RequestConflictError("Request payload changed")
                return
            total = conn.execute("SELECT count(*) FROM runtime_admissions").fetchone()[0]
            own = conn.execute(
                "SELECT count(*) FROM runtime_admissions a JOIN runtime_admission_owners o ON a.scope_key=o.scope_key WHERE o.owner_id=?",
                (context.user_id,),
            ).fetchone()[0]
            if (self.max_active_total is not None and total >= self.max_active_total) or (
                self.max_active_per_owner is not None and own >= self.max_active_per_owner
            ):
                raise ThreadBusyError("Account or service active request limit reached")
            conn.execute(
                "INSERT INTO runtime_admissions VALUES (?,?,?,NULL)",
                (context.scope_key, request_id, payload_sha256),
            )
            conn.execute(
                "INSERT OR REPLACE INTO runtime_admission_owners VALUES(?,?)",
                (context.scope_key, context.user_id),
            )

    def finish_run(self, context: ExecutionContext, run_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM runtime_admissions WHERE scope_key=? AND run_id=?",
                (context.scope_key, run_id),
            )

    def release_rejected_request(self, context: ExecutionContext, request_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE conversation_turns SET status='failed',updated_at=? WHERE scope_key=? AND request_id=? AND run_id IS NULL",
                (time.time(), context.scope_key, request_id),
            )
            conn.execute(
                "DELETE FROM runtime_admissions WHERE scope_key=? AND request_id=? "
                "AND run_id IS NULL",
                (context.scope_key, request_id),
            )

    def active_run(self, context: ExecutionContext) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT run_id FROM runtime_admissions WHERE scope_key=?", (context.scope_key,)
            ).fetchone()
        return row[0] if row else None
