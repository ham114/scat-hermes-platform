"""Owner-scoped chat journal; never reads native sessions, reasoning or credentials."""

import base64
import hashlib
import json
import math
import time
from contextlib import closing


def cursor(kind, scope, position):
    return (
        base64.urlsafe_b64encode(json.dumps([kind, scope, position]).encode()).decode().rstrip("=")
    )


def position(value, kind, scope):
    try:
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError()
        data = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(data, list) or len(data) != 3 or data[:2] != [kind, scope]:
            raise ValueError()
        return data[2]
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_history_cursor") from None


class ConversationHistory:
    """Mixin over the existing registry connection and ownership checks."""

    def initialize_history(self, db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_turns(
                id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, request_id TEXT NOT NULL,
                run_id TEXT UNIQUE, input TEXT, input_sha TEXT, status TEXT NOT NULL,
                output TEXT, created_at REAL, updated_at REAL, last_sequence INTEGER NOT NULL DEFAULT -1,
                UNIQUE(scope_key,request_id));
            CREATE INDEX IF NOT EXISTS conversation_turn_scope ON conversation_turns(scope_key,id);
            CREATE TABLE IF NOT EXISTS conversation_titles(
                thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL);
        """)
        # Original bindings contain no questions. Preserve that absence explicitly.
        db.execute("""INSERT OR IGNORE INTO conversation_turns(scope_key,request_id,run_id,status)
            SELECT scope_key,request_id,run_id,'unavailable' FROM runtime_runs ORDER BY rowid""")

    def record_question(self, db, context, request_id, message, digest):
        row = db.execute(
            "SELECT input_sha FROM conversation_turns WHERE scope_key=? AND request_id=?",
            (context.scope_key, request_id),
        ).fetchone()
        if row:
            if row[0] is not None and row[0] != digest:
                from hermes_runtime.registry import RequestConflictError

                raise RequestConflictError("Request payload changed")
            return
        db.execute(
            "INSERT INTO conversation_turns(scope_key,request_id,input,input_sha,status,created_at,updated_at) VALUES(?,?,?,?,'admitting',?,?)",
            (context.scope_key, request_id, message, digest, time.time(), time.time()),
        )
        title = " ".join(message.split())[:60]
        db.execute(
            "INSERT OR IGNORE INTO conversation_titles VALUES(?,?,1)", (context.thread_id, title)
        )

    def bind_history(self, db, context, request, run):
        db.execute(
            "INSERT OR IGNORE INTO conversation_turns(scope_key,request_id,run_id,status) VALUES(?,?,?,'unavailable')",
            (context.scope_key, request, run),
        )
        db.execute(
            "UPDATE conversation_turns SET status=CASE WHEN input IS NOT NULL AND run_id IS NULL THEN 'started' ELSE status END,run_id=? WHERE scope_key=? AND request_id=?",
            (run, context.scope_key, request),
        )

    def save_delta(self, context, run, sequence, delta):
        if type(sequence) is not int or sequence < 0 or not isinstance(delta, str):
            return
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT output,last_sequence,status FROM conversation_turns WHERE scope_key=? AND run_id=?",
                (context.scope_key, run),
            ).fetchone()
            if (
                not row
                or row[1] >= sequence
                or row[2] in {"completed", "failed", "cancelled", "interrupted"}
            ):
                return
            output = (row[0] or "") + delta
            if len(output.encode()) <= 2 * 1024**2:
                db.execute(
                    "UPDATE conversation_turns SET output=?,last_sequence=?,updated_at=? WHERE scope_key=? AND run_id=?",
                    (output, sequence, time.time(), context.scope_key, run),
                )

    def save_turn(self, context, data):
        self.require(context, data.get("run_id"))
        status = data.get("status")
        if status not in {
            "started",
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            return
        if status == "completed" and (
            data.get("completed") is False or data.get("partial") or data.get("interrupted")
        ):
            status = "interrupted"
        # Running snapshots have no SSE cursor. Mixing them with sequenced deltas
        # would duplicate text during concurrent history reads and reconnection.
        output = (
            data.get("output")
            if status in {"completed", "failed", "cancelled", "interrupted"}
            and isinstance(data.get("output"), str)
            else None
        )
        omitted = output is not None and len(output.encode()) > 2 * 1024**2
        if omitted:
            output = None
        with closing(self._connect()) as db, db:
            db.execute(
                """UPDATE conversation_turns SET status=?,output=CASE WHEN ? THEN NULL ELSE coalesce(?,output) END,updated_at=?
                WHERE scope_key=? AND run_id=? AND (status NOT IN ('completed','failed','cancelled','interrupted') OR (status=? AND output IS NULL))""",
                (status, omitted, output, time.time(), context.scope_key, data["run_id"], status),
            )

    def history_threads(self, owner, limit=20, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_history_limit")
        scope = hashlib.sha256(owner.encode()).hexdigest()
        values = [owner]
        clause = ""
        if after:
            key = position(after, "threads", scope)
            if (
                not isinstance(key, list)
                or len(key) != 2
                or type(key[0]) not in (int, float)
                or not math.isfinite(key[0])
                or not isinstance(key[1], str)
            ):
                raise ValueError("invalid_history_cursor")
            clause = " AND (t.created_at<? OR (t.created_at=? AND t.thread_id<?))"
            values.extend([key[0], key[0], key[1]])
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT t.thread_id,t.created_at,c.title,coalesce(c.revision,0) FROM runtime_threads t LEFT JOIN conversation_titles c ON c.thread_id=t.thread_id WHERE t.owner_id=?"
                + clause
                + " ORDER BY t.created_at DESC,t.thread_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        items = [
            dict(zip(("thread_id", "created_at", "title", "title_revision"), row))
            for row in rows[:limit]
        ]
        return {
            "items": items,
            "next_cursor": cursor("threads", scope, [rows[limit - 1][1], rows[limit - 1][0]])
            if len(rows) > limit
            else None,
        }

    def history_turns(self, context, limit=20, before=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_history_limit")
        key = position(before, "turns", context.scope_key) if before else 2**63 - 1
        if type(key) is not int or not 0 < key <= 2**63 - 1:
            raise ValueError("invalid_history_cursor")
        with closing(self._connect()) as db:
            db.row_factory = __import__("sqlite3").Row
            rows = db.execute(
                "SELECT id,request_id,run_id,input,output,status,created_at,updated_at,last_sequence FROM conversation_turns WHERE scope_key=? AND id<? ORDER BY id DESC LIMIT ?",
                (context.scope_key, key, limit + 1),
            ).fetchall()
        page = [dict(row) for row in rows[:limit]]
        for item in page:
            item["input_available"] = item["input"] is not None
        return {
            "items": list(reversed(page)),
            "next_cursor": cursor("turns", context.scope_key, page[-1]["id"])
            if len(rows) > limit
            else None,
        }

    def rename_thread(self, owner, thread, title, revision):
        self.require_thread(owner, thread)
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 100
            or any(ord(c) < 32 for c in title)
            or type(revision) is not int
            or revision < 0
        ):
            raise ValueError("invalid_thread_title")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT revision FROM conversation_titles WHERE thread_id=?", (thread,)
            ).fetchone()
            if (old[0] if old else 0) != revision:
                raise ValueError("thread_title_conflict")
            db.execute(
                "INSERT INTO conversation_titles VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET title=excluded.title,revision=excluded.revision",
                (thread, title.strip(), revision + 1),
            )
        return {"thread_id": thread, "title": title.strip(), "title_revision": revision + 1}
