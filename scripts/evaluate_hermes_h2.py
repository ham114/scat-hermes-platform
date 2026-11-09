"""Real DeepSeek + pinned Hermes + business MCP evaluation, with owned-profile cleanup.

Run with scat-hermes (Python 3.14). Instrumentation is confined to this process;
no evaluation routes or fault injection are added to the production launcher.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import statistics
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def failure_stack(exc):
    return [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(exc.__traceback__)[-8:]
    ]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


async def evaluate(args, report, report_path):
    if sys.version_info[:2] != (3, 14) or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use the dedicated Python 3.14 Anaconda environment")
    import httpx
    import run_hermes_api as launcher

    from hermes_runtime.evaluation import (
        business_payload,
        current_evaluation_row,
        evaluation_trace,
        history_payload,
        protected_head_preserved,
        provider_payload,
        score_boundaries,
        score_case,
        stop_and_settle,
    )

    identity = uuid4().hex[:12]
    home = ROOT / ".tmp/h12" / ("runtime-" + identity)
    home.mkdir(parents=True)
    os.environ.update(
        HERMES_HOME=str(home),
        HERMES_DISABLE_LAZY_INSTALLS="1",
        PYTHONDONTWRITEBYTECODE="1",
        TMPDIR=str(home),
        TEMP=str(home),
        TMP=str(home),
    )
    # Import upstream only after the owned home is set.
    sys.path.insert(0, str(ROOT / "vendor/hermes-agent"))
    import openai
    from gateway.platforms.api_server import APIServerAdapter
    from openai.resources.chat.completions import Completions

    corpus_path = ROOT / "evaluations/hermes_h2_v1.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    report.update(
        corpus_sha256=hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        upstream_commit="5b147ab036d6d19131bff8d20701b5001eab844a",
        profile=str(home.relative_to(ROOT)),
        cases=[],
        boundaries={},
        validation_fixture_version="3",
        implementation_sha256={
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [
                "scripts/evaluate_hermes_h2.py",
                "scripts/run_hermes_api.py",
                "src/hermes_runtime/evaluation.py",
                "src/application/readonly.py",
            ]
        },
    )
    report["stage"] = "initializing"
    save(report_path, report)
    current = {}
    adapters = []
    agents = []
    admitted_headers = {}
    original_init = APIServerAdapter.__init__
    original_create = APIServerAdapter._create_agent
    original_completion = Completions.create

    def traced_init(self, *pa, **kw):
        original_init(self, *pa, **kw)
        adapters.append(self)

    def traced_agent(self, *pa, **kw):
        row = current.get("row")
        old_progress = kw.get("tool_progress_callback")
        old_complete = kw.get("tool_complete_callback")

        def progress(event, tool=None, preview=None, arguments=None, **meta):
            if row is not None and event == "tool.started":
                row["calls"].append({"tool": tool, "arguments": arguments or {}})
            if old_progress:
                return old_progress(event, tool, preview, arguments, **meta)

        def complete(call_id, name, arguments, result):
            if row is not None:
                for call in reversed(row["calls"]):
                    if call["tool"] == name and "result" not in call:
                        call["result_type"] = type(result).__name__
                        call["result"] = business_payload(result)
                        break
            if old_complete:
                return old_complete(call_id, name, arguments, result)

        kw.update(tool_progress_callback=progress, tool_complete_callback=complete)
        agent = original_create(self, *pa, **kw)
        agents.append(agent)
        original_run = agent.run_conversation

        def isolated_run(*rargs, **rkwargs):
            with evaluation_trace(row):
                return original_run(*rargs, **rkwargs)

        agent.run_conversation = isolated_run
        if row is not None:
            row["effective_reasoning"] = agent.reasoning_config
            row["context_length"] = agent.context_compressor.context_length
            row["retry_limits"] = {
                "api_attempts": agent._api_max_retries,
                "automatic_recovery_cycles": agent._auto_recovery_cycles,
                # Pinned turn_api_error.py:366-370 guards and resets one primary cycle.
                "primary_transport_recovery_cycles": 1,
            }
        return agent

    def traced_completion(self, *pa, **kw):
        row = current_evaluation_row()
        if row is not None:
            body = provider_payload(kw)
            messages = body.get("messages", [])
            # Verify the actual tool result consumed by the provider, in addition to callbacks.
            tool_names = {
                t.get("id"): t.get("function", {}).get("name")
                for m in messages
                for t in m.get("tool_calls", [])
            }
            for message in messages:
                if message.get("role") == "tool":
                    name = tool_names.get(message.get("tool_call_id"))
                    payload = business_payload(message.get("content"))
                    if payload:
                        for call in reversed(row["calls"]):
                            if call["tool"] == name:
                                call["result"] = payload
                                call["result_observed_on_wire"] = True
                                break
            row["wire"].append(
                {
                    "model": kw.get("model"),
                    "stream": kw.get("stream", False),
                    "reasoning_effort": body.get("reasoning_effort"),
                    "request_keys": sorted(body),
                    "sdk_max_retries": getattr(self._client, "max_retries", None),
                    "extra_body": {
                        k: v
                        for k, v in (kw.get("extra_body") or {}).items()
                        if k in {"thinking", "reasoning"}
                    },
                    "tools": [t.get("function", {}).get("name") for t in body.get("tools", [])],
                    "tool_result_ids": [
                        m.get("tool_call_id") for m in messages if m.get("role") == "tool"
                    ],
                    "assistant_tool_ids": [
                        t.get("id") for m in messages for t in m.get("tool_calls", [])
                    ],
                    "assistant_reasoning_keys": [
                        "reasoning_content" in m for m in messages if m.get("role") == "assistant"
                    ],
                }
            )
            fault = row.get("fault_mode")
            if fault == "timeout_once":
                row["fault_mode"] = None
                row["injected_timeout"] = True
                raise openai.APITimeoutError(
                    request=httpx.Request("POST", "https://fixture.invalid")
                )
            if fault == "auth":
                row["injected_auth_failures"] = row.get("injected_auth_failures", 0) + 1
                response = httpx.Response(
                    401, request=httpx.Request("POST", "https://fixture.invalid")
                )
                raise openai.AuthenticationError(
                    "controlled validation rejection", response=response, body={}
                )
        return original_completion(self, *pa, **kw)

    APIServerAdapter.__init__ = traced_init
    APIServerAdapter._create_agent = traced_agent
    Completions.create = traced_completion
    server = None
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as check:
            try:
                await check.get(f"http://127.0.0.1:{args.port}/health")
            except httpx.TransportError:
                pass
            else:
                raise RuntimeError("validation_port_in_use")
        server = asyncio.create_task(
            launcher.serve(
                SimpleNamespace(
                    home=str(home.relative_to(ROOT)),
                    with_mcp=True,
                    synthetic_mcp=False,
                    port=args.port,
                )
            )
        )
        async with httpx.AsyncClient(trust_env=False, timeout=2) as check:
            for _ in range(120):
                if server.done():
                    await server
                    raise RuntimeError("validation_server_exited")
                try:
                    if (await check.get(f"http://127.0.0.1:{args.port}/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("validation_health_timeout")
        report["stage"] = "runtime_ready"
        save(report_path, report)
        key = (ROOT / "var/secrets/hermes-api.key").read_text().strip()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{args.port}",
            trust_env=False,
            headers={"Authorization": "Bearer " + key},
            timeout=180,
        ) as client:

            async def run(case, *, scope=None, extra=None):
                row = {"id": case["id"], "calls": [], "wire": [], "first_text_seconds": None}
                current["row"] = row
                row["fault_mode"] = current.pop("fault", None)
                scope = scope or "h12-" + uuid4().hex
                headers = {
                    "X-Hermes-Session-Key": scope,
                    "Idempotency-Key": scope + ":" + str(uuid4()),
                }
                started = time.monotonic()
                data = {"input": case["prompt"]} | (extra or {})
                response = await client.post("/v1/runs", json=data, headers=headers)
                response.raise_for_status()
                rid = response.json()["run_id"]
                row["run_id"] = rid
                admitted_headers[rid] = headers
                terminal = {}
                async with asyncio.timeout(240):
                    async with client.stream(
                        "GET", f"/v1/runs/{rid}/events", headers=headers
                    ) as stream:
                        stream.raise_for_status()
                        async for line in stream.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            event = json.loads(line[5:])
                            if (
                                event.get("event") == "message.delta"
                                and event.get("delta")
                                and row["first_text_seconds"] is None
                            ):
                                row["first_text_seconds"] = round(time.monotonic() - started, 4)
                            if event.get("event") in {
                                "run.completed",
                                "run.failed",
                                "run.cancelled",
                                "run.interrupted",
                            }:
                                terminal = event
                                break
                row["completion_seconds"] = round(time.monotonic() - started, 4)
                row["terminal"] = terminal.get("event", "missing")
                row["output"] = terminal.get("output", "")
                row["score"] = score_case(case, row["calls"], row["output"], row["terminal"])
                row["tool_roundtrip"] = any(
                    w["tool_result_ids"]
                    and set(w["tool_result_ids"]).issubset(w["assistant_tool_ids"])
                    for w in row["wire"]
                )
                return row

            for case in corpus["cases"]:
                if args.cases and case["id"] not in args.cases.split(","):
                    continue
                report["stage"] = case["id"]
                save(report_path, report)
                try:
                    row = await run(case)
                except Exception as exc:  # noqa: BLE001 -- preserve failures without logging credentials.
                    row = current.get("row", {"id": case["id"]})
                    row.update(
                        error_type=type(exc).__name__,
                        failure_stack=failure_stack(exc),
                        score=score_case(
                            case,
                            row.get("calls", []),
                            row.get("output", ""),
                            row.get("terminal", "missing"),
                        ),
                    )
                    # A timed-out run may still own its worker: stop it before the next case.
                    if row.get("run_id"):
                        try:
                            row["cancellation_status"] = await stop_and_settle(
                                client, row["run_id"], admitted_headers[row["run_id"]]
                            )
                            row["cancellation_settled"] = True
                        except Exception as cleanup_error:  # noqa: BLE001 -- stop the evaluation on unknown outcomes.
                            row["cancellation_settled"] = False
                            row["cancellation_error_type"] = type(cleanup_error).__name__
                            report["cases"].append(row)
                            save(report_path, report)
                            raise RuntimeError("validation_run_not_settled") from None
                report["cases"].append(row)
                save(report_path, report)
                print(
                    json.dumps(
                        {
                            "case": row["id"],
                            "passed": row["score"]["passed"],
                            "seconds": row.get("completion_seconds"),
                        }
                    ),
                    flush=True,
                )
            if args.boundaries:
                report["stage"] = "boundaries"
                save(report_path, report)
                scope = "h12-memory-" + identity
                first = await run(
                    {
                        "id": "B-memory-first",
                        "prompt": "记住代号 H12-" + identity + "，仅回复收到。",
                        "expected_tool": None,
                        "expected_arguments": {},
                        "evidence": [],
                    },
                    scope=scope,
                )
                second = await run(
                    {
                        "id": "B-memory-second",
                        "prompt": "仅原样回复刚才的代号，不调用工具。",
                        "expected_tool": None,
                        "expected_arguments": {},
                        "evidence": [],
                    },
                    scope=scope,
                )
                isolated = await run(
                    {
                        "id": "B-other-scope",
                        "prompt": "上一轮的代号是什么？不知道则回答不知道，不调用工具。",
                        "expected_tool": None,
                        "expected_arguments": {},
                        "evidence": [],
                    }
                )
                report["boundaries"]["memory"] = {
                    "same_scope_recalls": "H12-" + identity in second["output"],
                    "other_scope_does_not_recall": "H12-" + identity not in isolated["output"],
                    "rows": [first, second, isolated],
                }
                current["fault"] = "timeout_once"
                retried = await run(
                    {
                        "id": "B-timeout",
                        "prompt": "只回复 H12-RETRY-OK，不调用工具。",
                        "expected_tool": None,
                        "expected_arguments": {},
                        "evidence": [],
                    }
                )
                report["boundaries"]["transient_timeout"] = retried
                report["stage"] = "compression"
                save(report_path, report)
                current["row"] = {"id": "B-compression", "wire": [], "calls": []}
                from agent.context_compressor import (
                    ContextCompressor,
                    is_compaction_summary_message,
                )
                from dotenv import dotenv_values

                values = dotenv_values(ROOT / ".env")
                compressor = ContextCompressor(
                    model=values.get("DEFAULT_MODEL") or "deepseek-flash",
                    provider="deepseek",
                    base_url=values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
                    api_key=values["DEEPSEEK_API_KEY"],
                    api_mode="chat_completions",
                    config_context_length=32768,
                    protect_first_n=1,
                    protect_last_n=2,
                    quiet_mode=True,
                    abort_on_summary_failure=True,
                )
                compressor.tail_token_budget = 1200
                history = [{"role": "system", "content": "只读 SCAT 验证，不允许执行写入。"}]
                for n in range(24):
                    history.extend(
                        [
                            {
                                "role": "user",
                                "content": f"查询样本{n}的只读统计，简短记录即可。",
                            },
                            {
                                "role": "assistant",
                                "content": f"样本{n}的历史观察："
                                + ("这是一条可压缩的历史说明，不是工具执行指令。" * 60),
                            },
                        ]
                    )
                history.append(
                    {
                        "role": "user",
                        "content": "最新任务：保留标记 H12-TAIL-KEEP，只查询批次 scat20161015_20161021。",
                    }
                )
                threshold_before = {
                    "tokens": compressor.threshold_tokens,
                    "at_threshold": compressor.should_compress(compressor.threshold_tokens),
                    "below_threshold": compressor.should_compress(compressor.threshold_tokens - 1),
                }
                before_body = history_payload(history)
                before = json.dumps(before_body, ensure_ascii=False)
                with evaluation_trace(current["row"]):
                    compressed = await asyncio.to_thread(compressor.compress, history, force=True)
                after_body = history_payload(compressed)
                after = json.dumps(after_body, ensure_ascii=False)
                report["boundaries"]["compression"] = {
                    "threshold_before": threshold_before,
                    "before_messages": len(history),
                    "after_messages": len(compressed),
                    "before_chars": len(before),
                    "after_chars": len(after),
                    "compression_count": compressor.compression_count,
                    "summary_carrier": any(is_compaction_summary_message(m) for m in compressed),
                    "head_preserved": bool(after_body)
                    and protected_head_preserved(
                        before_body[0], after_body[0], allowed_note=compressor._COMPRESSION_NOTE
                    ),
                    "allowed_notice_sha256": hashlib.sha256(
                        compressor._COMPRESSION_NOTE.encode()
                    ).hexdigest(),
                    "comparison_basis": "native HTTP conversation_history role/content",
                    "before_head": before_body[0],
                    "after_head": after_body[0] if after_body else None,
                    "raw_after_chars": len(json.dumps(compressed, ensure_ascii=False)),
                    "tail_marker_preserved": "H12-TAIL-KEEP" in after,
                    "threshold_below": not compressor.should_compress(
                        compressor.threshold_tokens - 1
                    ),
                    "wire": current["row"]["wire"],
                }
                recalled = await run(
                    {
                        "id": "B-compressed-history",
                        "prompt": "仅原样回复最新任务中的 H12 标记，不调用工具。",
                        "expected_tool": None,
                        "expected_arguments": {},
                        "evidence": [],
                    },
                    extra={"conversation_history": after_body},
                )
                report["boundaries"]["compression"]["post_compression_recall"] = (
                    "H12-TAIL-KEEP" in recalled["output"]
                )
                report["boundaries"]["compression"]["continuation"] = recalled
                save(report_path, report)
                report["stage"] = "approval_fixture"
                save(report_path, report)
                # Controlled pending approval fixture; no command or business write is executed.
                from aiohttp import web
                from gateway.platforms.api_server import _approval_request_event
                from tools import approval
                from tools.approval_gateway_wait import _ApprovalEntry

                adapter = adapters[0]
                rid = first["run_id"]
                approval_key = "h12-approval-" + identity
                entry = _ApprovalEntry(
                    {"request_id": "h12-exact", "description": "controlled protocol fixture"}
                )
                approval._gateway_queues[approval_key] = [entry]
                adapter._run_approval_sessions[rid] = approval_key
                app = web.Application()
                app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", args.port + 1)
                try:
                    await site.start()
                    url = f"http://127.0.0.1:{args.port + 1}/v1/runs/{rid}/approval"
                    headers = {"X-Hermes-Session-Key": scope}
                    wrong = await client.post(
                        url, headers=headers, json={"choice": "deny", "request_id": "wrong"}
                    )
                    decision = await client.post(
                        url, headers=headers, json={"choice": "deny", "request_id": "h12-exact"}
                    )
                    replay = await client.post(
                        url, headers=headers, json={"choice": "deny", "request_id": "h12-exact"}
                    )
                    hidden = await client.post(
                        f"/v1/runs/{rid}/approval",
                        headers=headers,
                        json={"choice": "deny", "request_id": "h12-exact"},
                    )
                    approved_entry = _ApprovalEntry(
                        {"request_id": "h12-once", "description": "protocol fixture only"}
                    )
                    approval._gateway_queues[approval_key] = [approved_entry]
                    once = await client.post(
                        url, headers=headers, json={"choice": "once", "request_id": "h12-once"}
                    )
                    event = _approval_request_event(
                        rid, {"request_id": "h12-exact", "allow_session": False}
                    )
                    report["boundaries"]["approval"] = {
                        "fixture": "native pending queue; no dangerous tool execution",
                        "wrong_request_status": wrong.status_code,
                        "deny_status": decision.status_code,
                        "replay_status": replay.status_code,
                        "restricted_route_status": hidden.status_code,
                        "waiter_woken": entry.event.is_set(),
                        "result": entry.result,
                        "once_status": once.status_code,
                        "once_result": approved_entry.result,
                        "once_waiter_woken": approved_entry.event.is_set(),
                        "choices": event["choices"],
                        "event": event["event"],
                    }
                finally:
                    await runner.cleanup()
                    approval.unregister_gateway_notify(approval_key)
                    adapter._run_approval_sessions.pop(rid, None)
                # Run auth rejection last: its credential cooldown must not affect live compression.
                report["stage"] = "model_unavailable"
                save(report_path, report)
                current["fault"] = "auth"
                rejected = await run(
                    {
                        "id": "B-auth",
                        "prompt": "只回复 H12-SHOULD-NOT-SUCCEED。",
                        "expected_tool": None,
                        "expected_arguments": {},
                        "evidence": [],
                    }
                )
                current["fault"] = None
                report["boundaries"]["model_unavailable"] = rejected
            if args.boundaries:
                report["boundary_checks"] = score_boundaries(report["boundaries"])
            rows = report["cases"]
            total = len(rows)
            tool_ids = {c["id"] for c in corpus["cases"] if c["expected_tool"]}
            tool_rows = [r for r in rows if r["id"] in tool_ids]
            report["metrics"] = {
                "cases": total,
                "passed": sum(r["score"]["passed"] for r in rows),
                "tool_selection_accuracy": sum(
                    r["score"].get("tool_selection", False) for r in rows
                )
                / max(total, 1),
                "tool_cases": len(tool_rows),
                "positive_tool_selection_accuracy": sum(
                    r["score"].get("tool_selection", False) for r in tool_rows
                )
                / max(len(tool_rows), 1),
                "parameter_accuracy": sum(r["score"].get("parameters", False) for r in tool_rows)
                / max(len(tool_rows), 1),
                "first_text_observations": sum(
                    r.get("first_text_seconds") is not None for r in rows
                ),
                "error_rate": sum(not r["score"]["passed"] for r in rows) / max(total, 1),
                "refusal_cases_with_tool_execution": sum(
                    bool(r.get("calls"))
                    for r in rows
                    if next(c for c in corpus["cases"] if c["id"] == r["id"])["refusal"]
                ),
                "median_first_text_seconds": statistics.median(
                    [
                        r["first_text_seconds"]
                        for r in rows
                        if r.get("first_text_seconds") is not None
                    ]
                )
                if any(r.get("first_text_seconds") is not None for r in rows)
                else None,
                "median_completion_seconds": statistics.median(
                    [r["completion_seconds"] for r in rows if "completion_seconds" in r]
                )
                if any("completion_seconds" in r for r in rows)
                else None,
            }
            report["status"] = "completed"
    finally:
        current.clear()
        if server is not None:
            server.cancel()
            try:
                await server
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 -- preserve failures without logging credentials.
                report.update(status="failed", runtime_shutdown_error=type(exc).__name__)
        APIServerAdapter.__init__ = original_init
        APIServerAdapter._create_agent = original_create
        Completions.create = original_completion
        # Only remove the unique home created above, after runtime/MCP shutdown.
        resolved = home.resolve()
        if (
            resolved.parent == (ROOT / ".tmp/h12").resolve()
            and resolved.name == "runtime-" + identity
        ):
            shutil.rmtree(resolved)
        report["cleanup"] = {
            "owned_profile_removed": not home.exists(),
            "runtime_stopped": server is None or server.done(),
        }
        report["finished_at"] = time.time()
        save(report_path, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="")
    parser.add_argument("--boundaries", action="store_true")
    parser.add_argument("--port", type=int, default=18086)
    parser.add_argument("--report", default="h12-evaluation-attempt1.json")
    args = parser.parse_args()
    if Path(args.report).name != args.report or not args.report.endswith(".json"):
        parser.error("report must be a JSON basename")
    if not 1024 <= args.port <= 65534:
        parser.error("unprivileged ports required")
    known_cases = {
        item["id"]
        for item in json.loads(
            (ROOT / "evaluations/hermes_h2_v1.json").read_text(encoding="utf-8")
        )["cases"]
    }
    if args.cases and not set(args.cases.split(",")).issubset(known_cases):
        parser.error("unknown case ID; no model request was submitted")
    report_path = ROOT / "var/validation" / args.report
    if report_path.exists():
        parser.error("report already exists; preserve evidence with a new basename")
    report = {"status": "running", "started_at": time.time()}
    save(report_path, report)
    try:
        asyncio.run(evaluate(args, report, report_path))
    except Exception as exc:  # noqa: BLE001 -- preserve failures without logging credentials.
        report.update(
            status="failed", error_type=type(exc).__name__, failure_stack=failure_stack(exc)
        )
        save(report_path, report)
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
    if report.get("status") != "completed":
        raise SystemExit(1)
    # A completed execution is not a stage acceptance: all raw failures remain in the report.
    if any(not row["score"]["passed"] for row in report.get("cases", [])):
        raise SystemExit(2)

    if args.boundaries and not all(report.get("boundary_checks", {}).values()):
        raise SystemExit(2)
