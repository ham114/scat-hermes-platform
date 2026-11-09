"""Fixed-corpus scoring: inspect calls and returned evidence, never use an LLM judge."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from application.readonly import READ_ONLY_TOOLS


def business_payload(value: Any) -> dict:
    if isinstance(value, str):
        try:
            return business_payload(json.loads(value))
        except (ValueError, TypeError):
            return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, list | tuple):
        for item in value:
            found = business_payload(item)
            if found:
                return found
        return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("status"), str):
        return value
    nested = (
        value.get("structuredContent") or value.get("structured_content") or value.get("result")
    )
    if nested:
        return business_payload(nested)
    if isinstance(value.get("text"), str):
        return business_payload(value["text"])
    content = value.get("content", [])
    if isinstance(content, str):
        return business_payload(content)
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            found = business_payload(item.get("text"))
            if found:
                return found
    return {}


def find_values(value: Any, key: str) -> list:
    if isinstance(value, dict):
        return ([value[key]] if key in value else []) + [
            item for child in value.values() for item in find_values(child, key)
        ]
    if isinstance(value, list):
        return [item for child in value for item in find_values(child, key)]
    return []


def score_case(case: dict, calls: list[dict], output: str, terminal: str) -> dict:
    expected = case["expected_tool"]
    names = [item["tool"].removeprefix("mcp__scat__") for item in calls]
    selection = names == ([expected] if expected else [])
    parameters = selection
    if expected and selection:
        actual = calls[0].get("arguments", {})
        wanted = dict(case["expected_arguments"])
        if expected == "search_scat_knowledge":
            query = str(actual.get("query", ""))
            parameters = bool(query.strip()) and all(
                term.casefold() in query.casefold() for term in case.get("query_contains", [])
            )
            wanted["query"] = query
        try:
            schema = READ_ONLY_TOOLS[expected].schema
            parameters = parameters and schema.model_validate(actual) == schema.model_validate(
                wanted
            )
        except ValueError:
            parameters = False
    payloads = [business_payload(item.get("result")) for item in calls]
    evidence = True
    empty_reply = bool(
        re.search(r"未找到|没有找到|不存在|无匹配|没有匹配|未检索到|未查到|找不到", output)
    )
    for rule in case.get("evidence", []):
        if rule == "total":
            totals = [n for p in payloads for n in find_values(p, "total") if type(n) is int]
            evidence &= bool(totals) and any(
                re.search(r"(?<![0-9])" + str(n) + r"(?![0-9])", output.replace(",", ""))
                for n in totals
            )
        if rule == "not_found":
            evidence &= (
                bool(payloads)
                and all(p.get("status") == "not_found" for p in payloads)
                and empty_reply
            )
        if rule.startswith(("number:", "number_if_found:")):
            key = rule.split(":", 1)[1]
            missing = bool(payloads) and all(p.get("status") == "not_found" for p in payloads)
            if missing and rule.startswith("number_if_found:"):
                evidence &= empty_reply
            else:
                evidence_data = [p.get("data") for p in payloads]
                section = {
                    "get_scat_batch_overview": "statistics",
                    "inspect_scat_collection_data_quality": "summary",
                    "get_scat_track_points": "pagination",
                }.get(expected)
                if section:
                    evidence_data = [d.get(section) for d in evidence_data if isinstance(d, dict)]
                numbers = [n for d in evidence_data for n in find_values(d, key) if type(n) is int]
                evidence &= bool(numbers) and any(
                    re.search(r"(?<![0-9])" + str(n) + r"(?![0-9])", output.replace(",", ""))
                    for n in numbers
                )
        if rule in {"citation", "citation_if_present"}:
            sources = [
                s for p in payloads for s in find_values(p, "source_id") if isinstance(s, str)
            ]
            evidence &= (
                (bool(sources) and any(s in output for s in sources))
                if rule == "citation"
                else ((not sources and empty_reply) or any(s in output for s in sources))
            )
    statuses = [p.get("status", "missing_result") for p in payloads]
    tool_ok = all(s in {"ok", "not_found", "empty"} for s in statuses)
    refused = not case.get("refusal") or bool(
        re.search(r"不能|无法|不支持|未授权|没有.{0,8}权限|只读|不具备|不可|不提供|不会", output)
    )
    completed = terminal == "run.completed" and bool(output.strip())
    return {
        "tool_selection": selection,
        "parameters": parameters,
        "evidence": evidence,
        "tool_results_ok": tool_ok,
        "result_statuses": statuses,
        "refusal": refused,
        "completed": completed,
        "passed": all([selection, parameters, evidence, tool_ok, refused, completed]),
    }


def score_boundaries(boundaries: dict) -> dict[str, bool]:
    """Missing or failed boundary evidence never counts as a pass."""
    memory = boundaries.get("memory", {})
    retry = boundaries.get("transient_timeout", {})
    unavailable = boundaries.get("model_unavailable", {})
    compression = boundaries.get("compression", {})
    threshold = compression.get("threshold_before", {})
    approval = boundaries.get("approval", {})
    limits = retry.get("retry_limits", {})
    return {
        "scoped_history": memory.get("same_scope_recalls") is True
        and memory.get("other_scope_does_not_recall") is True,
        "bounded_retry": retry.get("injected_timeout") is True
        and limits.get("api_attempts") == 3
        and limits.get("automatic_recovery_cycles") == 0
        and limits.get("primary_transport_recovery_cycles") == 1
        and not retry.get("calls")
        and 2 <= len(retry.get("wire", [])) <= 6
        and all(w.get("sdk_max_retries") == 0 for w in retry.get("wire", []))
        and retry.get("terminal") == "run.completed"
        and retry.get("output", "").strip().strip("。.") == "H12-RETRY-OK",
        "model_unavailable": unavailable.get("terminal") == "run.failed"
        and 1 <= unavailable.get("injected_auth_failures", 0) <= 3
        and "H12-SHOULD-NOT-SUCCEED" not in unavailable.get("output", ""),
        "compression": compression.get("compression_count", 0) > 0
        and compression.get("after_chars", 1) < compression.get("before_chars", 0)
        and all(
            compression.get(k) is True
            for k in [
                "summary_carrier",
                "head_preserved",
                "tail_marker_preserved",
                "post_compression_recall",
            ]
        )
        and threshold.get("at_threshold") is True
        and threshold.get("below_threshold") is False,
        "native_approval_fixture": approval.get("wrong_request_status") == 409
        and approval.get("deny_status") == 200
        and approval.get("replay_status") == 409
        and approval.get("restricted_route_status") == 404
        and approval.get("result") == "deny"
        and approval.get("waiter_woken") is True
        and approval.get("once_status") == 200
        and approval.get("once_result") == "once"
        and approval.get("once_waiter_woken") is True
        and approval.get("event") == "approval.request"
        and approval.get("choices") == ["once", "deny"],
    }


def provider_payload(kwargs: dict) -> dict:
    """OpenAI SDK merges extra_body after typed kwargs; inspect that effective body."""
    extra = kwargs.get("extra_body")
    return kwargs | (extra if isinstance(extra, dict) else {})


_TRACE_ROW: ContextVar[dict | None] = ContextVar("hermes_evaluation_row", default=None)


@contextmanager
def evaluation_trace(row: dict | None):
    token = _TRACE_ROW.set(row)
    try:
        yield
    finally:
        _TRACE_ROW.reset(token)


def current_evaluation_row() -> dict | None:
    return _TRACE_ROW.get()


async def stop_and_settle(client, run_id: str, headers: dict, *, timeout=30, interval=0.2):
    """Stop once in the original scope; do not advance while its outcome is unknown."""
    response = await client.post(f"/v1/runs/{run_id}/stop", headers=headers, json={}, timeout=5)
    response.raise_for_status()
    deadline = time.monotonic() + timeout
    while True:
        response = await client.get(f"/v1/runs/{run_id}", headers=headers, timeout=5)
        response.raise_for_status()
        state = response.json()
        if state.get("run_id") != run_id:
            raise RuntimeError("validation_run_mismatch")
        if state.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            return state["status"]
        if time.monotonic() >= deadline:
            raise RuntimeError("validation_run_not_settled")
        await asyncio.sleep(interval)


def history_payload(messages: list[dict]) -> list[dict]:
    """Match the native HTTP conversation_history role/content projection."""
    return [{"role": str(m["role"]), "content": str(m["content"])} for m in messages]


def protected_head_preserved(before: dict, after: dict, *, allowed_note: str) -> bool:
    """Permit only the pinned engine's exact notice after the intact protected head."""
    if before.get("role") != after.get("role") or not before.get("content"):
        return False
    if after.get("content") == before["content"]:
        return True
    return (
        before["role"] == "system"
        and bool(allowed_note)
        and after.get("content") == (before["content"] + "\n\n" + allowed_note)
    )
