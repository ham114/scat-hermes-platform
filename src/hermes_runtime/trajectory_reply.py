"""Deterministic coverage facts from scoped receipts; never infer totals from prose."""

from __future__ import annotations

import re


def coverage(store, scope, run_id):
    groups = {}
    for ref in store.list(scope, run_id):
        if ref["operation"] != "get_scat_track_points":
            continue
        result = store.get(scope, run_id, ref["result_id"])["result"]
        if result.get("display_only") or result.get("representation") == "page_summary":
            continue
        query = result.get("query", {})
        data = result.get("data") or {}
        page, points = data.get("pagination", {}), data.get("points")
        start, step, total = query.get("start"), query.get("every_nth", 1), page.get("total_points")
        if (
            not isinstance(points, list)
            or any(type(v) is not int or v < 0 for v in [start, total])
            or type(step) is not int
            or step < 1
        ):
            continue
        key = (
            query.get("batch"),
            query.get("external_id"),
            query.get("start_time"),
            query.get("end_time"),
        )
        group = groups.setdefault(
            key, {"flight": str(query.get("external_id", "")), "totals": set(), "indices": set()}
        )
        group["totals"].add(total)
        group["indices"].update(start + i * step for i in range(len(points)))
    facts = []
    for group in groups.values():
        total = next(iter(group["totals"])) if len(group["totals"]) == 1 else None
        indices = group["indices"]
        facts.append(
            {
                "flight": group["flight"],
                "loaded": len(indices),
                "total": total,
                "complete": total is not None
                and len(indices) == total
                and (not indices or min(indices) == 0 and max(indices) == total - 1),
            }
        )
    return facts


def normalize_reply(text, facts):
    if not text or not facts:
        return text
    # Idempotence for API polling/history reads of an already normalized answer.
    if text.startswith("数据范围（系统核验）"):
        text = text.split("\n\n", 1)[1] if "\n\n" in text else ""
    partial = any(not item["complete"] for item in facts)
    paragraphs = text.split("\n\n")
    if partial:
        paragraphs = [
            p
            for p in paragraphs
            if not re.search(
                r"(?:读取|读完|取得|获取|加载)[^。\n]{0,24}(?:全部|全量|完整)|(?:全部|全量|完整)[^。\n]{0,16}(?:读取|读完)",
                p,
            )
        ]
    sentences = [
        f"航班{f['flight']}用于本次分析的轨迹明细已读{f['loaded']}/{f['total'] if f['total'] is not None else '未知'}点，{'已完整覆盖当前查询窗口' if f['complete'] else '仅覆盖部分点或抽样点，不代表完整逐点航迹'}。"
        for f in facts
    ]
    return "数据范围（系统核验）：" + "".join(sentences) + "\n\n" + "\n\n".join(paragraphs)


def attach_reply_guard(agent, store, scope, run_id):
    original = agent.run_conversation

    def guarded(**kwargs):
        result = original(**kwargs)
        if (
            isinstance(result, dict)
            and not result.get("failed")
            and isinstance(result.get("final_response"), str)
        ):
            facts = coverage(store, scope, run_id)
            result = {**result, "final_response": normalize_reply(result["final_response"], facts)}
            messages = result.get("messages")
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                result["messages"] = [
                    *messages[:-1],
                    {**messages[-1], "content": result["final_response"]},
                ]
        return result

    agent.run_conversation = guarded
