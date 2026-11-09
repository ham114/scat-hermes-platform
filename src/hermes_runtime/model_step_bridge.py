"""Native coarse model tools. Trusted session bindings never come from LLM arguments."""

import json
from pathlib import Path

from hermes_runtime.task_bridge import TaskToolBridge

MODEL_STEP_TOOLS = frozenset(
    {
        "scat_model_prepare_input",
        "scat_model_run",
        "scat_model_evaluate",
        "scat_model_save_result",
        "scat_model_read_result",
    }
)


class ModelStepBridge(TaskToolBridge):
    def bind_job(self, session_id, scope, run_id, capability):
        self.bind(session_id, scope, run_id)
        if not hasattr(self, "_job_sessions"):
            self._job_sessions = {}
        self._job_sessions[session_id] = capability

    def schemas(self):
        schemas = self._invoke(schemas=True)
        if not isinstance(schemas, list) or {x.get("name") for x in schemas} != MODEL_STEP_TOOLS:
            raise RuntimeError("Model step schema allowlist mismatch")
        catalog = json.loads(
            (
                Path(__file__).resolve().parents[2] / "services/hermes/model-operations.v1.json"
            ).read_text(encoding="utf-8")
        )
        expected = [
            {
                "name": row["name"],
                "description": row["description"],
                "parameters": row["parameters"],
            }
            for row in catalog["operations"]
        ]
        if schemas != expected:
            raise RuntimeError("Model step schema differs from reviewed v1 contract")
        return schemas

    def handler(self, name):
        if name not in MODEL_STEP_TOOLS:
            raise ValueError("Unknown model step")

        def invoke(args, *, session_id="", **kwargs):
            with self._lock:
                context = self._sessions.get(session_id)
            if not context:
                return json.dumps({"status": "forbidden", "error": "trusted_step_context_required"})
            return json.dumps(
                self._invoke(
                    {
                        "operation": name,
                        "arguments": args,
                        "scope": context[0],
                        "source_run_id": context[1],
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
