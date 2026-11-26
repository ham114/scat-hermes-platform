"""Owner-bound diagnostic journal and a bounded, read-only Langfuse adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "succeeded", "partial"}


def configuration():
    name = os.environ.get("SCAT_LANGFUSE_CONFIG")
    if not name:
        return None
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("diagnostic_configuration_path")
    config = json.loads(path.read_text())
    if not config.get("enabled", True):
        return None
    return config


def configure_native():
    config = configuration()
    if config:
        for field, variable in {
            "base_url": "HERMES_LANGFUSE_BASE_URL",
            "public_key": "HERMES_LANGFUSE_PUBLIC_KEY",
            "secret_key": "HERMES_LANGFUSE_SECRET_KEY",
        }.items():
            os.environ[variable] = config[field]
        os.environ["HERMES_LANGFUSE_CAPTURE"] = "metadata"
        os.environ["HERMES_LANGFUSE_ENV"] = "production"
    return bool(config)


def prepare_native_plugin(home):
    config = configuration()
    if not config:
        return
    source = (ROOT / config["plugin_path"]).resolve()
    if not source.is_relative_to(ROOT):
        raise ValueError("langfuse_plugin_path")
    lock = json.loads((ROOT / "services/hermes/langfuse.lock.json").read_text())
    target = Path(home) / "plugins/langfuse"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, expected in lock["files"].items():
        body = (source / name).read_bytes()
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError("langfuse_plugin_source_changed")
        path = target / name
        if path.is_symlink():
            raise ValueError("langfuse_plugin_link_rejected")
        path.write_bytes(body)
        path.chmod(0o600)


def journal():
    if not configuration():
        return None
    path = ROOT / os.environ.get("SCAT_DIAGNOSTICS_STATE_FILE", "var/platform/diagnostics.sqlite")
    return DiagnosticStore(path, root=ROOT)


def record(method, *args, **kwargs):
    """Diagnostics are supplementary. A failed journal must not fail a business call."""
    try:
        store = journal()
        if store:
            return getattr(store, method)(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- optional tracing must never fail business execution.
        logger.warning("diagnostic_journal_unavailable error_type=%s", type(exc).__name__)
    return None


def attach_native_outcome(agent, scope, run):
    from gateway.platforms.api_server_runs import terminal_run_status

    original = agent.run_conversation

    def traced(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except BaseException:
            record("finish", scope, run, "interrupted")
            raise
        body = result if isinstance(result, dict) else {}
        status, _ = terminal_run_status(body)
        record("finish", scope, run, "failed" if body.get("failed") else status)
        return result

    agent.run_conversation = traced


def summary(value):
    """Capture structure and business status, never credentials or arbitrary text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {"type": "text", "chars": len(value)}
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    result = {"keys": list(value)[:30]}
    from hermes_runtime.diagnostic_errors import describe_failure

    failure = describe_failure(value)
    if failure:
        result["error"] = failure
    for key in ("status", "operation", "result_id", "payload_sha256", "artifact_id", "task_id"):
        item = value.get(key)
        if (
            isinstance(item, str)
            and len(item) <= 200
            and all(c.isalnum() or c in "_.:-" for c in item)
        ):
            result[key] = item
    for key in ("batch", "external_id", "metric", "model_key", "step_key"):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", item):
            result[key] = item
    for key in (
        "start",
        "limit",
        "skip",
        "every_nth",
        "gap_s",
        "max_ground_speed_mps",
        "max_vertical_rate_mps",
    ):
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item):
            result[key] = item
    if isinstance(value.get("channels"), list):
        result["channels"] = [
            item
            for item in value["channels"][:6]
            if isinstance(item, str) and re.fullmatch(r"[a-z0-9_]{1,100}", item)
        ]
    if isinstance(value.get("fields"), list):
        result["validation_fields"] = [
            [part for part in path if isinstance(part, (str, int))]
            for path in value["fields"][:20]
            if isinstance(path, list)
        ]
    data = value.get("data")
    if isinstance(data, dict):
        result["counts"] = {k: len(v) for k, v in data.items() if isinstance(v, list)}
        if isinstance(data.get("pagination"), dict):
            result["pagination"] = {
                k: v for k, v in data["pagination"].items() if type(v) in (int, bool) or v is None
            }
    return result


class DiagnosticStore:
    def __init__(self, path, *, root):
        self.path = Path(path).resolve()
        if not self.path.is_relative_to(Path(root).resolve()) or self.path == Path(root).resolve():
            raise ValueError("diagnostic_state_path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS diagnostic_runs (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, run_id TEXT NOT NULL,
                owner TEXT, thread_id TEXT, request_id TEXT, session_id TEXT,
                profile TEXT, label TEXT, state TEXT NOT NULL DEFAULT 'running',
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(scope,run_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS diagnostic_nodes (
                id TEXT NOT NULL, trace_id TEXT NOT NULL, parent_id TEXT,
                name TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                started REAL NOT NULL, ended REAL, input TEXT NOT NULL, output TEXT NOT NULL,
                PRIMARY KEY(trace_id,id))""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS diagnostic_owner_time ON diagnostic_runs(owner,created)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS diagnostic_tasks (
                task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL,
                thread_id TEXT NOT NULL, source_run TEXT, backend_run TEXT NOT NULL)""")
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def identifier(scope, run):
        return hashlib.sha256((scope + ":" + run).encode()).hexdigest()[:32]

    def bind(
        self,
        scope,
        run,
        *,
        session_id=None,
        profile=None,
        owner=None,
        thread_id=None,
        request_id=None,
        label=None,
    ):
        identity = self.identifier(scope, run)
        now = time.time()
        with closing(self.connect()) as db, db:
            existing = db.execute(
                "SELECT owner,thread_id,request_id,session_id FROM diagnostic_runs WHERE id=?",
                (identity,),
            ).fetchone()
            if existing:
                for key, value in {
                    "owner": owner,
                    "thread_id": thread_id,
                    "request_id": request_id,
                    "session_id": session_id,
                }.items():
                    if value is not None and existing[key] is not None and existing[key] != value:
                        raise ValueError("diagnostic_binding_conflict")
            db.execute(
                """INSERT INTO diagnostic_runs(id,scope,run_id,owner,thread_id,request_id,session_id,profile,label,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,run_id) DO UPDATE SET
                owner=COALESCE(diagnostic_runs.owner,excluded.owner),
                thread_id=COALESCE(diagnostic_runs.thread_id,excluded.thread_id),
                request_id=COALESCE(diagnostic_runs.request_id,excluded.request_id),
                session_id=COALESCE(diagnostic_runs.session_id,excluded.session_id),
                profile=COALESCE(diagnostic_runs.profile,excluded.profile),
                label=COALESCE(diagnostic_runs.label,excluded.label),updated=excluded.updated""",
                (
                    identity,
                    scope,
                    run,
                    owner,
                    thread_id,
                    request_id,
                    session_id,
                    profile,
                    label,
                    now,
                    now,
                ),
            )
        return identity

    def finish(self, scope, run, state, *, authoritative=False):
        if state not in TERMINAL:
            return
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE diagnostic_runs SET state=?,updated=? WHERE scope=? AND run_id=? AND (state='running' OR ?)",
                (state, time.time(), scope, run, int(authoritative)),
            )

    def node(
        self,
        scope,
        run,
        key,
        name,
        *,
        state,
        started,
        input=None,
        output=None,
        kind="span",
        parent_id=None,
    ):
        trace = self.bind(scope, run)
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO diagnostic_nodes VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trace_id,id) DO UPDATE SET state=excluded.state,
                ended=excluded.ended,output=excluded.output""",
                (
                    key,
                    trace,
                    parent_id,
                    name,
                    kind,
                    state,
                    started,
                    time.time() if state != "running" else None,
                    json.dumps(summary(input), ensure_ascii=False),
                    json.dumps(summary(output), ensure_ascii=False),
                ),
            )

    def get(self, identity, owner, *, admin=False):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM diagnostic_runs WHERE id=? AND (owner=? OR ?)",
                (identity, owner, int(admin)),
            ).fetchone()
        if row is None:
            raise LookupError("diagnostic_not_found")
        return dict(row)

    def list(
        self, owner, *, admin=False, limit=20, before=None, state=None, thread_id=None, run_id=None
    ):
        predicates, values = ["(owner=? OR ?)"], [owner, int(admin)]
        if before:
            predicates.append("(created,id)<(?,?)")
            values.extend(before)
        for key, value in [("state", state), ("thread_id", thread_id), ("run_id", run_id)]:
            if value:
                predicates.append(key + "=?")
                values.append(value)
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM diagnostic_runs WHERE "
                + " AND ".join(predicates)
                + " ORDER BY created DESC,id DESC LIMIT ?",
                [*values, limit + 1],
            ).fetchall()
        return [dict(row) for row in rows[:limit]], len(rows) > limit

    def nodes(self, identity):
        from hermes_runtime.diagnostic_errors import describe_failure

        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM diagnostic_nodes WHERE trace_id=? ORDER BY started,id LIMIT 500",
                (identity,),
            ).fetchall()
        nodes = [
            {
                **dict(row),
                "input": json.loads(row["input"]),
                "output": json.loads(row["output"]),
                "source": "platform",
            }
            for row in rows
        ]
        for node in nodes:
            failure = describe_failure(node["output"])
            if failure:
                node["output"]["error"] = failure
                node["failure"] = failure
        return nodes

    def bind_task(self, context, row):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO diagnostic_tasks VALUES(?,?,?,?,?,?)",
                (
                    row["task_id"],
                    context.scope_key,
                    context.user_id,
                    context.thread_id,
                    row["snapshot"]["source"].get("run_id"),
                    row["run_id"],
                ),
            )

    def task(self, task_id, owner, *, admin=False):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM diagnostic_tasks WHERE task_id=? AND (owner=? OR ?)",
                (task_id, owner, int(admin)),
            ).fetchone()
        if row is None:
            raise LookupError("diagnostic_task_not_found")
        return dict(row)

    def related_tasks(self, row):
        with closing(self.connect()) as db:
            return [
                value[0]
                for value in db.execute(
                    "SELECT task_id FROM diagnostic_tasks WHERE scope=? AND source_run=? LIMIT 50",
                    (row["scope"], row["run_id"]),
                )
            ]


class LangfuseReader:
    def __init__(self, config):
        self.config = config

    async def observations(self, session_id):
        import asyncio

        from hermes_runtime.diagnostic_errors import describe_failure

        if not session_id:
            return [], "pending"
        config = self.config
        try:
            async with (
                asyncio.timeout(15),
                httpx.AsyncClient(
                    base_url=config["base_url"],
                    auth=(config["public_key"], config["secret_key"]),
                    timeout=6,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
            ):
                nodes, cursor = [], None
                for _ in range(5):
                    params = {
                        "sessionId": session_id,
                        "limit": 100,
                        "fields": "core,basic,time,usage,model,metadata,trace_context",
                    }
                    if cursor:
                        params["cursor"] = cursor
                    reply = await client.get("/api/public/v2/observations", params=params)
                    reply.raise_for_status()
                    document = reply.json()
                    for node in document["data"]:
                        # A backend query never grants access. Verify every returned session.
                        if node.get("sessionId") != session_id:
                            continue
                        details = {
                            key: value
                            for key, value in (node.get("metadata") or {}).items()
                            if key
                            in {
                                "tool_name",
                                "tool_call_id",
                                "retry_count",
                                "max_retries",
                                "retryable",
                                "status_code",
                                "finish_reason",
                                "capture_mode",
                                "error_type",
                            }
                            and type(value) in (str, int, bool)
                            and len(str(value)) <= 200
                        }
                        message = node.get("statusMessage")
                        if isinstance(message, str) and re.fullmatch(
                            r"[A-Za-z0-9_.:-]{1,100}", message
                        ):
                            details["error_type"] = message
                        nodes.append(
                            {
                                "id": node["id"],
                                "parent_id": node.get("parentObservationId"),
                                "name": node.get("name", "调用"),
                                "kind": node.get("type", "SPAN").lower(),
                                "state": "failed"
                                if node.get("level") == "ERROR"
                                else "completed"
                                if node.get("endTime")
                                else "unknown",
                                "started": node.get("startTime"),
                                "ended": node.get("endTime"),
                                "model": node.get("model"),
                                "usage": node.get("usageDetails") or {},
                                "cost": node.get("costDetails") or {},
                                "input": {},
                                "output": {},
                                "source": "langfuse",
                                "langfuse_trace_id": node["traceId"],
                                "details": details,
                                "failure": describe_failure(
                                    {
                                        "status": "model_api_error"
                                        if node.get("type") == "GENERATION"
                                        else "tool_error",
                                        "error": {
                                            **details,
                                            "exception_type": details.get("error_type"),
                                        },
                                    }
                                )
                                if node.get("level") == "ERROR"
                                else None,
                            }
                        )
                    cursor = (document.get("meta") or {}).get("cursor")
                    if not cursor:
                        break
                nodes = list({node["id"]: node for node in nodes}.values())
                return nodes, "truncated" if cursor else "available" if nodes else "pending"
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            return [], "unavailable"
