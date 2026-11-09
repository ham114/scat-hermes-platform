"""Separate main controls and explicitly delegated background workflow tools."""

import json
from pathlib import Path

from hermes_runtime.model_step_bridge import ModelStepBridge
from workflow_runtime.catalog import CONTROLS, OPERATIONS


class WorkflowBridge(ModelStepBridge):
    def __init__(self, *args, controls=False, operations=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.controls = controls
        self.operations = set(CONTROLS if controls else operations or OPERATIONS)
        if self.operations - set(CONTROLS if controls else OPERATIONS):
            raise ValueError("Unreviewed workflow operation")

    def schemas(self):
        rows = self._invoke(schemas=True)
        expected = json.loads(
            (
                Path(__file__).resolve().parents[2] / "services/hermes/workflow-operations.v1.json"
            ).read_text(encoding="utf-8")
        )["controls" if self.controls else "operations"]
        expected = [{k: r[k] for k in ("name", "description", "parameters")} for r in expected]
        if rows != expected:
            raise RuntimeError("Workflow schemas differ from reviewed contract")
        return [r for r in rows if r["name"] in self.operations]

    def handler(self, name):
        if name not in self.operations:
            raise ValueError("Unknown delegated workflow operation")

        # Reuse the trusted session transport, with this reviewed namespace.
        def invoke(args, *, session_id="", **kwargs):
            with self._lock:
                context = self._sessions.get(session_id)
            if not context:
                return json.dumps(
                    {"status": "forbidden", "error": "trusted_workflow_context_required"}
                )
            from hashlib import sha256

            arguments_sha = sha256(
                json.dumps(
                    args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                ).encode()
            ).hexdigest()
            return json.dumps(
                self._invoke(
                    {
                        "operation": name,
                        "arguments": args,
                        "scope": context[0],
                        "source_run_id": context[1],
                        "proposal_key": context[1] + ":" + arguments_sha,
                        **(
                            {"job_session": self._job_sessions[session_id]}
                            if session_id in getattr(self, "_job_sessions", {})
                            else {}
                        ),
                    }
                ),
                ensure_ascii=False,
            )

        return invoke
