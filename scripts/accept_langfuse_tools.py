"""Bounded real-tool acceptance of the official Langfuse hooks, without an LLM call."""

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261126-langfuse"
sys.path.insert(0, str(ROOT / ".tmp/langfuse/native"))


async def main():
    from application.context import ExecutionContext
    from application.readonly_extensions import build_extended_business_application
    from hermes_runtime import diagnostics
    from hermes_runtime.registry import RunRegistry
    from hermes_runtime.results import ToolResultStore, publish_tool_result

    diagnostics.ROOT = ROOT
    os.environ["SCAT_LANGFUSE_CONFIG"] = "var/deploy/langfuse/client.json"
    os.environ["SCAT_DIAGNOSTICS_STATE_FILE"] = "var/deploy/h7b/shared/state/diagnostics.sqlite"
    os.environ["HERMES_HOME"] = str(ROOT / ".tmp/langfuse/acceptance-home")
    Path(os.environ["HERMES_HOME"]).mkdir(exist_ok=True)
    assert diagnostics.configure_native()
    config = diagnostics.configuration()
    spec = importlib.util.spec_from_file_location(
        "scat_langfuse_acceptance", ROOT / ".tmp/langfuse/official/__init__.py"
    )
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    hooks = {}
    plugin.register(
        SimpleNamespace(
            profile_name="scat-main",
            current_cron_execution=lambda: None,
            register_hook=lambda name, fn: hooks.setdefault(name, fn),
        )
    )
    assert {"pre_tool_call", "post_tool_call"}.issubset(hooks)
    state = ROOT / "var/deploy/h7b/shared/state"
    registry = RunRegistry(state / "bindings.sqlite", workspace_root=ROOT)
    receipts = ToolResultStore(state / "results.sqlite", workspace_root=ROOT)
    thread = registry.create_thread("scat-researcher")["thread_id"]
    context = ExecutionContext("scat-researcher", thread)
    journal = diagnostics.journal()
    business = build_extended_business_application()
    outcomes = []
    cases = [
        ({"batch": "scat20161015_20161021", "external_id": "100002", "limit": 20}, "ok"),
        (
            {"batch": "scat20161015_20161021", "external_id": "100002", "limit": 101},
            "invalid_request",
        ),
        ({"batch": "qa-no-such-batch-20261126", "external_id": "100002", "limit": 1}, "not_found"),
    ]
    try:
        for arguments, expected in cases:
            run, session, call = "run_" + uuid4().hex, "lf-qa-" + uuid4().hex, "qa-" + uuid4().hex
            registry.bind(context, str(uuid4()), run)
            identity = journal.bind(
                context.scope_key,
                run,
                owner=context.user_id,
                thread_id=thread,
                session_id=session,
                profile="scat-main",
                label="真实轨迹工具验收 · " + expected,
            )
            common = {"session_id": session, "turn_id": run}
            # The legacy turn-start hook opens only a root. No model generation is fabricated.
            hooks["pre_llm_call"](**common, messages=[])
            hooks["pre_tool_call"](
                **common,
                tool_name="mcp__scat__get_scat_track_points",
                args=arguments,
                tool_call_id=call,
            )
            started = time.time()
            result = await business.invoke("get_scat_track_points", arguments, context=context)
            assert result["status"] == expected, result["status"]
            hooks["post_tool_call"](
                **common,
                tool_name="mcp__scat__get_scat_track_points",
                args=arguments,
                result=result,
                tool_call_id=call,
                status="ok" if expected == "ok" else "error",
                error_type=expected,
            )
            journal.node(
                context.scope_key,
                run,
                "result:" + call,
                "轨迹工具返回",
                started=started,
                state="completed" if expected == "ok" else "failed",
                input=arguments,
                output=result,
                kind="tool-result",
            )
            reference = publish_tool_result(
                receipts,
                scope_key=context.scope_key,
                run_id=run,
                call_id=call,
                tool="mcp__scat__get_scat_track_points",
                result=result,
            )
            if expected == "ok":
                assert reference
                receipt = receipts.get(context.scope_key, run, reference["result_id"])
                assert receipt["payload_sha256"] == reference["payload_sha256"]
            else:
                assert reference is None
            hooks["on_session_finalize"](session_id=session, reason="acceptance")
            journal.finish(context.scope_key, run, "completed" if expected == "ok" else "failed")
            outcomes.append(
                {
                    "trace_id": identity,
                    "run_id": run,
                    "session_id": session,
                    "status": expected,
                    "reference": reference,
                }
            )
        client = plugin._get_langfuse()
        assert client is not None, "plugin_client_not_initialized"
        client.flush()
        reader = diagnostics.LangfuseReader(config)
        for outcome in outcomes:
            for attempt in range(30):
                nodes, status = await reader.observations(outcome["session_id"])
                if status == "available" and any(node["kind"] == "tool" for node in nodes):
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError("langfuse_readback_" + status)
            assert not any(node["kind"] == "generation" for node in nodes)
            tool = next(node for node in nodes if node["kind"] == "tool")
            assert tool["state"] == ("completed" if outcome["status"] == "ok" else "failed")
            outcome["langfuse_nodes"] = len(nodes)
            outcome.pop("session_id")
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "real-tool-acceptance.json").write_text(
            json.dumps(
                {
                    "mode": "real_business_calls_and_official_hooks",
                    "external_llm_calls": 0,
                    "thread_id": thread,
                    "outcomes": outcomes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "tools": [row["status"] for row in outcomes],
                    "traces": [row["trace_id"] for row in outcomes],
                    "external_llm_calls": 0,
                }
            ),
            flush=True,
        )
    finally:
        await business.aclose()
        plugin.on_session_finalize(reason="shutdown")


if __name__ == "__main__":
    asyncio.run(main())
