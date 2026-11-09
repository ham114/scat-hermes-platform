"""Opt-in knowledge operation: trusted skill/run context and durable, no-replay execution."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from hermes_runtime.real_rag import load_config, load_corpus, select_mode

TOOL = "mcp__scat__search_scat_knowledge"
PYTHON = os.environ.get("SCAT_HERMES_PYTHON", "/home/h3c/anaconda3/envs/scat-hermes/bin/python")


def knowledge_run_scope(principal: str, session_key: str) -> str:
    """Add trusted platform conversation scope to the native key/profile principal."""
    return hashlib.sha256(json.dumps([principal, session_key.strip()]).encode()).hexdigest()


def process_identity(pid: int):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text.rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class RagOperation:
    def __init__(self, root, state_root, bridge, config_path, *, executor=None):
        self.root = Path(root).resolve()
        self.state_root = Path(state_root).resolve()
        self.config_path = Path(config_path).resolve()
        if not self.state_root.is_relative_to(
            self.root / "var"
        ) or not self.config_path.is_relative_to(self.root):
            raise ValueError("rag_operation_path_outside_project")
        self.config = load_config(self.config_path)
        self.records = load_corpus(self.config)
        self.config_sha = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        self.bridge = bridge
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_root.chmod(0o700)
        self.path = self.state_root / "invocations.sqlite"
        self.executor = executor or self._execute
        self._questions = {}
        self._question_lock = threading.RLock()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS rag_invocations (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, run_id TEXT NOT NULL,
                request_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL, output TEXT NOT NULL,
                pid INTEGER, birth TEXT, result TEXT, error TEXT, started REAL NOT NULL, finished REAL)""")
        self.path.chmod(0o600)

    def bind_agent_question(self, agent):
        """Bind the trusted API turn, not model-authored search keywords, to deep research."""
        original = agent.run_conversation

        def run(*args, **kwargs):
            question = kwargs.get("user_message", args[0] if args else None)
            session_id = agent.session_id
            trusted, _ = self.trusted(session_id)
            if not isinstance(question, str):
                return original(*args, **kwargs)
            with self._question_lock:
                if session_id in self._questions:
                    raise ValueError("knowledge_turn_already_running")
                self._questions[session_id] = (trusted, question)
            try:
                return original(*args, **kwargs)
            finally:
                with self._question_lock:
                    self._questions.pop(session_id, None)

        agent.run_conversation = run

    def question_for(self, session_id, query):
        with self._question_lock:
            saved = self._questions.get(session_id)
        if saved is None:
            return query
        trusted, _ = self.trusted(session_id)
        if trusted != saved[0]:
            raise ValueError("knowledge_question_owner_changed")
        if not 1 <= len(saved[1]) <= 1000:
            raise ValueError("knowledge_question_exceeds_limit")
        return saved[1]

    def trusted(self, session_id):
        selected = self.bridge.session_bridge(session_id)
        trusted = selected._sessions.get(session_id)
        budget = self.bridge._budgets.get(session_id)
        if not trusted or budget is None or (budget.scope, budget.run_id) != tuple(trusted):
            raise ValueError("trusted_parent_run_and_budget_required")
        return trusted, budget

    def _row(self, identity):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM rag_invocations WHERE id=?", (identity,)).fetchone()
            return dict(row) if row else None

    def _update(self, identity, **values):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE rag_invocations SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), identity),
            )

    def owned_process(self, row):
        if not row or not row["pid"] or process_identity(row["pid"]) != row["birth"]:
            return False
        try:
            args = Path(f"/proc/{row['pid']}/cmdline").read_bytes().split(b"\0")
            return (
                args[0].decode() == PYTHON
                and str(self.root / "scripts/run_real_agent_rag.py").encode() in args
                and row["output"].encode() in args
            )
        except OSError:
            return False

    def status(self, identity, session_id):
        (scope, _run), _budget = self.trusted(session_id)
        row = self._row(identity)
        if row is None or row["scope"] != scope:
            return {"status": "not_found"}
        if row["status"] in {"starting", "running"} and not self.owned_process(row):
            # A child can be between spawn/registration, or have exited just before
            # its live controller validates the result. A duplicate must not turn
            # either transient state into a permanent unknown outcome.
            import fcntl

            with (self.state_root / "execution.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    self._update(
                        identity,
                        status="unknown",
                        error="controller_or_child_interrupted_do_not_replay",
                    )
                    row = self._row(identity)
        if row["status"] == "succeeded":
            try:
                cached = json.loads(row["result"])
                if hashlib.sha256(cached["answer"].encode()).hexdigest() != cached["answer_sha256"]:
                    raise ValueError("cached_answer_digest_mismatch")
            except (ValueError, TypeError, KeyError):
                self._update(identity, status="unknown", error="stored_rag_result_corrupt")
                return {"status": "unknown", "request_id": identity, "automatic_replay": False}
            return {
                "status": "ok",
                "request_id": identity,
                "data": cached,
                "reused": True,
            }
        return {
            "status": row["status"],
            "request_id": identity,
            "error": row["error"],
            "automatic_replay": False,
        }

    def invoke(self, query, mode, session_id):
        import fcntl

        (scope, run_id), budget = self.trusted(session_id)
        mode = select_mode(query, mode, self.config)
        snapshot = budget.snapshot()
        if snapshot["exhausted"] or snapshot["finished_at"] is not None:
            return {"status": "budget_exhausted"}
        request_hash = hashlib.sha256(
            json.dumps([scope, run_id, query, mode, self.config_sha], ensure_ascii=False).encode()
        ).hexdigest()
        with closing(sqlite3.connect(self.path)) as db:
            old = db.execute(
                "SELECT id FROM rag_invocations WHERE request_hash=?", (request_hash,)
            ).fetchone()
        if old:
            return self.status(old[0], session_id)
        with (self.state_root / "execution.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy", "message": "已有知识研究运行，请稍后重试。"}
            # Recheck after taking the process-wide lock.
            with closing(sqlite3.connect(self.path)) as db:
                old = db.execute(
                    "SELECT id FROM rag_invocations WHERE request_hash=?", (request_hash,)
                ).fetchone()
            if old:
                return self.status(old[0], session_id)
            identity = "rag-" + uuid4().hex
            job = self.state_root / identity
            output = self.root / "var/validation/rag-operations" / identity
            job.mkdir(mode=0o700)
            config = copy.deepcopy(self.config) | {"scope": scope}
            local_config = job / "config.json"
            local_config.write_text(json.dumps(config, ensure_ascii=False))
            request = job / "request.json"
            request.write_text(json.dumps({"question": query, "mode": mode}, ensure_ascii=False))
            parent = job / "parent-budget.json"
            parent.write_text(
                json.dumps(
                    {
                        "path": str(budget.path),
                        "scope": scope,
                        "run_id": run_id,
                        "limits": budget.limits.model_dump(),
                        "policy_sha256": snapshot["policy_sha256"],
                    }
                )
            )
            for path in (local_config, request, parent):
                path.chmod(0o600)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(
                    "INSERT INTO rag_invocations(id,scope,run_id,request_hash,status,output,started) VALUES(?,?,?,?,?,?,?)",
                    (identity, scope, run_id, request_hash, "starting", str(output), time.time()),
                )
            command = [
                PYTHON,
                "-B",
                str(self.root / "scripts/run_real_agent_rag.py"),
                "--config",
                str(local_config),
                "--request",
                str(request),
                "--shared-budget",
                str(parent),
                "--output",
                str(output),
            ]
            try:
                report = self.executor(command, identity, budget)
                if report.get("status") != "passed" or report.get("mode") != mode:
                    raise ValueError("rag_child_did_not_complete")
                result = report["result"]
                if hashlib.sha256(result["answer"].encode()).hexdigest() != result["answer_sha256"]:
                    raise ValueError("rag_answer_digest_mismatch")
                if any(
                    e["reference"]["scope"] != scope
                    for c in result["claims"]
                    for e in c["evidence"]
                ):
                    raise ValueError("rag_child_scope_mismatch")
                for claim in result["claims"]:
                    for citation in claim["evidence"]:
                        reference = citation["reference"]
                        source = self.records.get(reference["chunk_id"])
                        if (
                            source is None
                            or reference["run_id"] != report["run_id"]
                            or reference["corpus_sha256"] != self.config["corpus_sha256"]
                            or any(
                                reference[k] != source[k]
                                for k in ("source_id", "source_version", "text_sha256")
                            )
                            or not citation["quote"]
                            or source["text"][citation["start_char"] : citation["end_char"]]
                            != citation["quote"]
                        ):
                            raise ValueError("rag_child_evidence_mismatch")
                public = {
                    k: result[k]
                    for k in (
                        "answer",
                        "status",
                        "claims",
                        "targets",
                        "coverage",
                        "receipt_id",
                        "answer_sha256",
                    )
                }
                encoded = json.dumps(public, ensure_ascii=False)
                if len(encoded.encode()) > 60000:
                    raise ValueError("rag_answer_output_too_large")
                with closing(sqlite3.connect(self.path)) as db, db:
                    changed = db.execute(
                        "UPDATE rag_invocations SET status='succeeded',result=?,finished=? WHERE id=? AND status IN ('starting','running')",
                        (encoded, time.time(), identity),
                    ).rowcount
                if changed != 1:
                    return self.status(identity, session_id)
                return {
                    "status": "ok",
                    "request_id": identity,
                    "data": public,
                    "mode": mode,
                    "reused": False,
                }
            except (
                OSError,
                ValueError,
                RuntimeError,
                TimeoutError,
                subprocess.SubprocessError,
                KeyError,
                TypeError,
            ):
                self._update(
                    identity,
                    status="unknown",
                    error="rag_execution_not_verified_do_not_replay",
                    finished=time.time(),
                )
                return {"status": "unknown", "request_id": identity, "automatic_replay": False}

    def _execute(self, command, identity, budget):
        row = self._row(identity)
        if row["status"] != "starting":
            raise RuntimeError("rag_cancelled_before_launch")
        output = Path(row["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with (self.state_root / identity / "process.log").open("w") as log:
            process = subprocess.Popen(
                command, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            owned = row | {"pid": process.pid, "birth": process_identity(process.pid)}
            try:
                with closing(sqlite3.connect(self.path)) as db, db:
                    changed = db.execute(
                        "UPDATE rag_invocations SET status='running',pid=?,birth=? WHERE id=? AND status='starting'",
                        (owned["pid"], owned["birth"], identity),
                    ).rowcount
                if changed != 1:
                    raise RuntimeError("rag_cancelled_during_launch")
                while process.poll() is None:
                    state = budget.snapshot()
                    if (
                        state["exhausted"]
                        or state["finished_at"] is not None
                        or time.time() - state["started_at"] >= budget.limits.wall_seconds
                    ):
                        raise TimeoutError("parent_budget_ended")
                    time.sleep(0.2)
                if process.returncode:
                    raise RuntimeError("rag_process_failed")
                return json.loads((output / "result.json").read_text())
            finally:
                if process.poll() is None and self.owned_process(owned):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if self.owned_process(owned):
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()

    def cancel(self, identity, session_id):
        (scope, _run), _budget = self.trusted(session_id)
        row = self._row(identity)
        if row is None or row["scope"] != scope:
            return {"status": "not_found"}
        if row["status"] not in {"starting", "running"}:
            return self.status(identity, session_id)
        if self.owned_process(row):
            os.killpg(row["pid"], signal.SIGTERM)
        self._update(
            identity,
            status="unknown",
            error="cancel_requested_verify_final_state",
            finished=time.time(),
        )
        return {"status": "unknown", "request_id": identity, "automatic_replay": False}


def register_knowledge_rag(registry, bridge, root, state_root, config_path):
    """Extend the existing governed knowledge operation; keep its original name."""
    if getattr(getattr(registry, "_scat_skill_guard", None), "bridge", None) is not bridge:
        raise ValueError("knowledge_rag_requires_installed_skill_guard")
    operation = RagOperation(root, state_root, bridge, config_path)
    entry = registry.get_entry(TOOL)
    if entry is None or entry.is_async:
        raise ValueError("expected_pinned_synchronous_knowledge_tool")
    schema = copy.deepcopy(entry.schema)
    schema["parameters"]["properties"]["mode"] = {
        "type": "string",
        "enum": ["fast", "deep", "auto"],
        "default": "fast",
        "description": "fast使用原检索；deep启用预算内多Agent研究；auto按问题规则路由。",
    }
    original = entry.handler

    def invoke(arguments, *, session_id="", **kwargs):
        arguments = dict(arguments)
        mode = arguments.pop("mode", "fast")
        if (
            set(arguments) - {"query", "limit"}
            or not isinstance(arguments.get("query"), str)
            or type(arguments.get("limit", 5)) is not int
            or not 1 <= arguments.get("limit", 5) <= 10
        ):
            return json.dumps({"status": "invalid_request"})
        try:
            question = (
                arguments["query"]
                if mode == "fast"
                else operation.question_for(session_id, arguments["query"])
            )
            chosen = select_mode(question, mode, operation.config)
            if chosen == "fast":
                return original(arguments, session_id=session_id, **kwargs)
            result = operation.invoke(question, chosen, session_id)
        except (ValueError, KeyError):
            result = {"status": "forbidden", "error": "trusted_knowledge_context_required"}
        return json.dumps(result, ensure_ascii=False)

    registry.register(
        name=TOOL,
        toolset=entry.toolset,
        schema=schema,
        handler=invoke,
        check_fn=entry.check_fn,
        requires_env=entry.requires_env,
        override=True,
        execution_timeout=365,
    )
    return operation
