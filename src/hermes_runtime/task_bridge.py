"""Pinned Hermes native-tool bridge. Backend code runs only in its Conda Python."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from threading import Lock

TASK_TOOLS = {
    "scat_task_catalog",
    "scat_task_prepare",
    "scat_task_list",
    "scat_task_get",
    "scat_task_artifact",
}


class TaskToolBridge:
    def __init__(self, python, script, *, env, timeout=35):
        self.python = str(python)
        self.script = str(script)
        self.env = env
        self.timeout = timeout
        self._sessions = {}
        self._lock = Lock()

    def bind(self, session_id, scope, run_id):
        if not session_id or not scope or not run_id:
            raise ValueError("Task tools require trusted session, scope and run")
        with self._lock:
            known = self._sessions.get(session_id)
            if known and known[0] != scope:
                raise ValueError("Native session scope changed")
            if not known and len(self._sessions) >= 1000:
                raise ValueError("Task session capacity reached; restart runtime")
            self._sessions[session_id] = (scope, run_id)

    def _invoke(self, payload=None, *, schemas=False):
        if self.env.get("SCAT_ACTION_BASE_URL"):
            from deployment_runtime.client import action_request

            namespace = {
                "run_model_task_tool.py": "tasks",
                "run_model_step_tool.py": "steps",
                "run_workflow_tool.py": "workflows",
            }.get(Path(self.script).name)
            if namespace is None:
                raise ValueError("unreviewed_private_action_namespace")
            return action_request(
                Path(self.script).resolve().parents[1],
                self.env,
                namespace,
                payload or {},
                schemas=schemas,
                timeout=self.timeout,
            )
        try:
            proc = subprocess.run(
                [self.python, "-B", self.script] + (["--schemas"] if schemas else []),
                input=json.dumps(payload or {}, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=self.env,
                timeout=self.timeout,
                check=False,
            )
            if proc.returncode or len(proc.stdout.encode()) > 524288:
                return {"status": "unavailable", "message": "Model task backend is unavailable"}
            return json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {"status": "unavailable", "message": "Model task backend is unavailable"}

    def schemas(self):
        schemas = self._invoke(schemas=True)
        if not isinstance(schemas, list) or {x.get("name") for x in schemas} != TASK_TOOLS:
            raise RuntimeError("Task schema allowlist mismatch")
        return schemas

    def handler(self, name):
        if name not in TASK_TOOLS:
            raise ValueError("Unknown task tool")

        def invoke(args, *, session_id="", **kwargs):
            with self._lock:
                trusted = self._sessions.get(session_id)
            if trusted is None:
                return json.dumps(
                    {"status": "forbidden", "message": "Trusted task context is missing"}
                )
            scope, run_id = trusted
            # v0.22.3 registry dispatch forwards session_id, but omits tool_call_id.
            # Same proposal in one trusted run is idempotent even when the LLM repeats it.
            try:
                argument_hash = hashlib.sha256(
                    json.dumps(
                        args,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
            except (TypeError, ValueError):
                return json.dumps({"status": "rejected", "message": "Invalid task arguments"})
            return json.dumps(
                self._invoke(
                    {
                        "operation": name,
                        "arguments": args,
                        "scope": scope,
                        "source_run_id": run_id,
                        "proposal_key": run_id + ":" + argument_hash,
                    }
                ),
                ensure_ascii=False,
            )

        return invoke
