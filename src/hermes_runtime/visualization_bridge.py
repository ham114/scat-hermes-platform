"""Run-bound visualization controls; code execution stays in the separate worker."""

import json

from hermes_runtime.task_bridge import TaskToolBridge

VISUALIZATION_TOOLS = {
    "create_visualization",
    "revise_visualization",
    "get_visualization_status",
    "list_visualizations",
    "list_visualization_sources",
}


class VisualizationBridge(TaskToolBridge):
    def schemas(self):
        rows = self._invoke(schemas=True)
        if not isinstance(rows, list) or {r.get("name") for r in rows} != VISUALIZATION_TOOLS:
            raise RuntimeError("visualization_schema_allowlist_mismatch")
        return rows

    def handler(self, name):
        if name not in VISUALIZATION_TOOLS:
            raise ValueError("unknown_visualization_tool")

        def invoke(args, *, session_id="", **kwargs):
            with self._lock:
                trusted = self._sessions.get(session_id)
            if trusted is None:
                return json.dumps(
                    {"status": "forbidden", "error": "trusted_visualization_context_required"}
                )
            return json.dumps(
                self._invoke(
                    {
                        "operation": name,
                        "arguments": args,
                        "scope": trusted[0],
                        "source_run_id": trusted[1],
                    }
                ),
                ensure_ascii=False,
            )

        return invoke
