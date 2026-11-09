"""Conda child IPC, durable execution receipts and parent-owned tool capabilities."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from threading import Event, Timer
from uuid import UUID, uuid4

from hermes_runtime.script_language import ScriptError, encode, parse, validate_value
from hermes_runtime.skills import checked_path


def sha(value):
    encode(value)
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ScriptStore:
    def __init__(self, path, root, *, recover_namespace=None):
        self.path = checked_path(root, path.relative_to(root).as_posix())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_script_runs(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, run TEXT NOT NULL,
                    request TEXT NOT NULL, fingerprint TEXT NOT NULL, metadata TEXT NOT NULL,
                    state TEXT NOT NULL, output TEXT, created REAL NOT NULL, finished REAL,
                    UNIQUE(scope,run,request));
                CREATE TABLE IF NOT EXISTS skill_script_calls(
                    execution TEXT NOT NULL, seq INTEGER NOT NULL, operation TEXT NOT NULL,
                    arguments_sha TEXT NOT NULL, state TEXT NOT NULL, result_sha TEXT,
                    PRIMARY KEY(execution,seq));
            """)
            # A worker's shared task database may contain another live job.
            # Only fence the restarting job, never unrelated worker namespaces.
            clause, values = (
                (" AND scope=? AND run=?", tuple(recover_namespace))
                if recover_namespace is not None
                else ("", ())
            )
            db.execute(
                "UPDATE skill_script_runs SET state='unknown',finished=? WHERE state='running'"
                + clause,
                (time.time(), *values),
            )
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, scope, run, request, fingerprint, metadata, policy):
        request = str(UUID(str(request)))
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM skill_script_runs WHERE scope=? AND run=? AND request=?",
                (scope, run, request),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ScriptError("script_request_conflict")
                if row["state"] != "succeeded":
                    raise ScriptError("script_" + row["state"] + "_no_replay")
                return dict(row), True
            if db.execute(
                "SELECT 1 FROM skill_script_runs WHERE scope=? AND run=? AND state IN ('running','unknown')",
                (scope, run),
            ).fetchone():
                raise ScriptError("script_unresolved_execution")
            if (
                db.execute("SELECT count(*) FROM skill_script_runs").fetchone()[0]
                >= policy.max_records
            ):
                raise ScriptError("script_receipt_capacity")
            if (
                db.execute(
                    "SELECT count(*) FROM skill_script_runs WHERE scope=? AND run=?", (scope, run)
                ).fetchone()[0]
                >= policy.max_runs_per_run
            ):
                raise ScriptError("script_run_budget_exhausted")
            identity = uuid4().hex
            db.execute(
                "INSERT INTO skill_script_runs VALUES(?,?,?,?,?,?,'running',NULL,?,NULL)",
                (identity, scope, run, request, fingerprint, encode(metadata), time.time()),
            )
            return dict(
                db.execute("SELECT * FROM skill_script_runs WHERE id=?", (identity,)).fetchone()
            ), False

    def finish(self, identity, state, output=None):
        with closing(self.connect()) as db, db:
            return (
                db.execute(
                    "UPDATE skill_script_runs SET state=?,output=?,finished=? WHERE id=? AND state='running'",
                    (state, encode(output) if output is not None else None, time.time(), identity),
                ).rowcount
                == 1
            )

    def admit_call(self, identity, seq, operation, arguments):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM skill_script_runs WHERE id=? AND state='running'", (identity,)
            ).fetchone():
                raise ScriptError("script_execution_fenced")
            db.execute(
                "INSERT INTO skill_script_calls VALUES(?,?,?,?,'admitted',NULL)",
                (identity, seq, operation, sha(arguments)),
            )

    def returned(self, identity, seq, result):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE skill_script_calls SET state='returned',result_sha=? WHERE execution=? AND seq=?",
                (sha(result), identity, seq),
            )


class ScriptRunner:
    def __init__(self, root, store, policy):
        self.root, self.store, self.policy = root.resolve(), store, policy

    def run(
        self,
        *,
        scope,
        run,
        request_id,
        source,
        descriptor,
        inputs,
        metadata,
        dispatch,
        check=lambda: None,
        deadline=None,
    ):
        policy = self.policy
        if not policy.enabled:
            raise ScriptError("skill_scripts_disabled")
        if os.name != "posix" or not (self._prefix() / "conda-meta").is_dir():
            raise ScriptError("script_existing_linux_conda_required")
        encode(inputs, policy.max_input_bytes)
        validate_value(inputs, descriptor["input_schema"])
        parse(source, descriptor["operations"])
        fingerprint = sha(
            {
                "source": source,
                "descriptor": descriptor,
                "inputs": inputs,
                "metadata": metadata,
                "policy": policy.sha256,
            }
        )
        check()
        row, replayed = self.store.claim(scope, run, request_id, fingerprint, metadata, policy)
        if replayed:
            return json.loads(row["output"]) | {"replayed": True}
        identity, calls, receipts = row["id"], 0, []
        folder = checked_path(self.root, ".tmp/skill-scripts/" + identity)
        folder.mkdir(parents=True)
        process, timer = None, None
        expired = Event()
        result = None
        try:
            # Only trusted interpreter code is launched; user source is JSON stdin.
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(self.root / "scripts/run_skill_script_child.py"),
                ],
                cwd=folder,
                env={
                    "LANG": "C.UTF-8",
                    "HOME": str(folder),
                    "TMPDIR": str(folder),
                    "TMP": str(folder),
                    "TEMP": str(folder),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            def terminate():
                expired.set()
                self.store.finish(identity, "unknown")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            remaining = (
                min(policy.wall_seconds, deadline - time.time())
                if deadline
                else policy.wall_seconds
            )
            if remaining <= 0:
                raise ScriptError("script_wall_exhausted")
            timer = Timer(remaining, terminate)
            timer.daemon = True
            timer.start()
            initial = {
                "source": source,
                "inputs": inputs,
                "limits": policy.model_dump(),
                "descriptor": {
                    k: descriptor[k] for k in ("operations", "input_schema", "output_schema")
                },
            }
            self._send(process, initial, policy.max_frame_bytes)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if expired.is_set() or not selector.select(policy.wall_seconds):
                        raise ScriptError("script_wall_exhausted")
                    raw = process.stdout.readline(policy.max_frame_bytes + 1)
                    if not raw or len(raw) > policy.max_frame_bytes or not raw.endswith(b"\n"):
                        raise ScriptError("script_child_protocol_invalid")
                    message = json.loads(raw)
                    if message.get("type") == "error":
                        raise ScriptError("script_child_rejected")
                    if message.get("type") == "result" and set(message) == {"type", "output"}:
                        check()
                        validate_value(message["output"], descriptor["output_schema"])
                        encode(message["output"], policy.max_output_bytes)
                        result = {
                            "success": True,
                            "execution_id": identity,
                            "replayed": False,
                            "output": message["output"],
                            "tool_calls": calls,
                            "business_result_references": receipts,
                            "metadata": metadata,
                        }
                        if expired.is_set() or not self.store.finish(identity, "succeeded", result):
                            raise ScriptError("script_execution_fenced")
                        return result
                    if (
                        set(message) != {"type", "seq", "operation", "arguments"}
                        or message["type"] != "call"
                        or type(message["seq"]) is not int
                        or message["seq"] != calls + 1
                        or message["operation"] not in descriptor["operations"]
                        or type(message["arguments"]) is not dict
                    ):
                        raise ScriptError("script_child_protocol_invalid")
                    calls += 1
                    if calls > policy.max_tool_calls or expired.is_set():
                        raise ScriptError("script_tool_budget_exhausted")
                    check()
                    self.store.admit_call(
                        identity, calls, message["operation"], message["arguments"]
                    )
                    value, reference = dispatch(
                        message["operation"], message["arguments"], identity, calls
                    )
                    encode(value, policy.max_frame_bytes - 100)
                    self.store.returned(identity, calls, value)
                    if reference:
                        receipts.append(reference)
                    check()
                    if expired.is_set():
                        raise ScriptError("script_wall_exhausted")
                    self._send(process, {"seq": calls, "result": value}, policy.max_frame_bytes)
        except Exception:  # noqa: BLE001 -- preserve unknown across backend/lease/budget failures
            self.store.finish(identity, "unknown" if calls or expired.is_set() else "failed")
            raise ScriptError(
                "script_outcome_unknown" if calls or expired.is_set() else "script_execution_failed"
            ) from None
        finally:
            if timer:
                timer.cancel()
            if process:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
            if (
                folder.resolve().parent == (self.root / ".tmp/skill-scripts").resolve()
                and not folder.is_symlink()
            ):
                shutil.rmtree(folder)

    @staticmethod
    def _prefix():
        from pathlib import Path

        return Path(sys.prefix)

    @staticmethod
    def _send(process, value, maximum):
        process.stdin.write((encode(value, maximum) + "\n").encode())
        process.stdin.flush()
