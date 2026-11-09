"""Bounded, scoped read-only tool receipts; separate from model-task artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from application.readonly_extensions import ALL_READ_ONLY_TOOLS as READ_ONLY_TOOLS

_SCOPE = re.compile(r"scat:[0-9a-f]{64}\Z")
_RUN = re.compile(r"run_[A-Za-z0-9_-]{1,128}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")


def tool_payload(value: Any, depth: int = 0) -> dict | None:
    """Unwrap only known MCP envelopes, with bounded input and recursion."""
    if depth > 8:
        return None
    if isinstance(value, str):
        if len(value.encode()) > 262144:
            return None
        try:
            return tool_payload(json.loads(value), depth + 1)
        except (ValueError, TypeError):
            return None
    if isinstance(value, dict):
        if isinstance(value.get("status"), str) and "data" in value:
            return value
        for key in ("structuredContent", "result", "content", "text"):
            if key in value:
                found = tool_payload(value[key], depth + 1)
                if found is not None:
                    return found
    if isinstance(value, list):
        for item in value[:16]:
            found = tool_payload(item, depth + 1)
            if found is not None:
                return found
    return None


class ResultNotFoundError(LookupError):
    pass


class ToolResultStore:
    """One bounded receipt per native tool call, retained for one day by default."""

    def __init__(
        self,
        path: Path,
        *,
        workspace_root: Path,
        ttl: float = 86400,
        max_rows: int = 1000,
        max_bytes: int = 65536,
    ):
        root = workspace_root.resolve()
        self.path = path.resolve()
        if self.path == root or not self.path.is_relative_to(root) or path.is_symlink():
            raise ValueError("Result store must remain inside the project")
        if ttl <= 0 or max_rows < 1 or max_bytes < 1:
            raise ValueError("Invalid result retention budget")
        self.ttl, self.max_rows, self.max_bytes = ttl, max_rows, max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tool_results (
                result_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, run_id TEXT NOT NULL,
                call_id TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
                sha256 TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                UNIQUE(scope_key, run_id, call_id))""")
        self.path.chmod(0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def _scope(scope_key, run_id):
        if not isinstance(scope_key, str) or not _SCOPE.fullmatch(scope_key):
            raise ValueError("Invalid result scope")
        if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
            raise ValueError("Invalid result run")

    @staticmethod
    def reference(row):
        return {
            "result_id": row[0],
            "operation": row[4],
            "payload_sha256": row[6],
            "created_at": row[7],
            "expires_at": row[8],
            "kind": "readonly_tool_result",
        }

    def put(self, scope_key, run_id, call_id, operation, payload):
        self._scope(scope_key, run_id)
        if (
            operation not in READ_ONLY_TOOLS
            or not isinstance(call_id, str)
            or not 1 <= len(call_id) <= 200
        ):
            raise ValueError("Invalid tool receipt identity")
        if not isinstance(payload, dict) or payload.get("status") != "ok" or "data" not in payload:
            raise ValueError("Only successful business results can be published")
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(body.encode()) > self.max_bytes:
            raise ValueError("Tool receipt exceeds budget")
        digest = hashlib.sha256(body.encode()).hexdigest()
        now = time.time()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM tool_results WHERE expires_at <= ?", (now,))
            row = conn.execute(
                "SELECT * FROM tool_results WHERE scope_key=? AND run_id=? AND call_id=?",
                (scope_key, run_id, call_id),
            ).fetchone()
            if row is not None:
                if row[4] != operation or row[6] != digest:
                    raise ValueError("Tool receipt changed")
                return self.reference(row)
            if conn.execute("SELECT count(*) FROM tool_results").fetchone()[0] >= self.max_rows:
                raise ValueError("Tool receipt storage is full")
            row = (
                uuid4().hex,
                scope_key,
                run_id,
                call_id,
                operation,
                body,
                digest,
                now,
                now + self.ttl,
            )
            conn.execute("INSERT INTO tool_results VALUES (?,?,?,?,?,?,?,?,?)", row)
            return self.reference(row)

    def get(self, scope_key, run_id, result_id):
        self._scope(scope_key, run_id)
        if not isinstance(result_id, str) or not _ID.fullmatch(result_id):
            raise ResultNotFoundError("Result not found")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM tool_results WHERE result_id=? AND scope_key=? AND run_id=? AND expires_at>?",
                (result_id, scope_key, run_id, time.time()),
            ).fetchone()
        if row is None:
            raise ResultNotFoundError("Result not found")
        if hashlib.sha256(row[5].encode()).hexdigest() != row[6]:
            raise ValueError("Tool receipt integrity check failed")
        return self.reference(row) | {"result": json.loads(row[5])}

    def list(self, scope_key, run_id):
        self._scope(scope_key, run_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM tool_results WHERE scope_key=? AND run_id=? AND expires_at>? ORDER BY created_at,result_id",
                (scope_key, run_id, time.time()),
            ).fetchall()
        return [self.reference(row) for row in rows]


def publish_tool_result(store, *, scope_key, run_id, call_id, tool, result):
    """Input comes from the pinned agent's completion callback, never model text."""
    prefix = "mcp__scat__"
    if not isinstance(tool, str) or not tool.startswith(prefix):
        return None
    operation = tool.removeprefix(prefix)
    if operation not in READ_ONLY_TOOLS:
        return None
    payload = tool_payload(result)
    if payload is None or payload.get("status") != "ok":
        return None
    return store.put(scope_key, run_id, call_id, operation, payload)
