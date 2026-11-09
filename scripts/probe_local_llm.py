"""Bounded text and synthetic tool-calling probes for an OpenAI-compatible LLM.

This script does not connect to the business database or execute a model-selected tool.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any


def probe(base_url: str, model: str, timeout: float) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    def request(name: str, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 192,
            "chat_template_kwargs": {"enable_thinking": False},
            **extra,
        }
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.load(response)
        elapsed = time.monotonic() - started
        message = payload["choices"][0]["message"]
        results.append(
            {
                "name": name,
                "elapsed_seconds": round(elapsed, 3),
                "message": message,
                "finish_reason": payload["choices"][0].get("finish_reason"),
                "usage": payload.get("usage"),
                "timings": payload.get("timings"),
            }
        )
        return message

    arithmetic = request(
        "arithmetic",
        [{"role": "user", "content": "计算17乘23，只输出十进制整数答案。"}],
    )
    results[-1]["passed"] = str(arithmetic.get("content", "")).strip() == "391"
    chinese = request(
        "chinese_explanation",
        [
            {
                "role": "user",
                "content": "用中文两句话解释飞行轨迹预测中ADE与FDE的区别，不编造实验数字。",
            }
        ],
    )
    content = str(chinese.get("content", ""))
    results[-1]["passed"] = "ADE" in content and "FDE" in content and any(
        "\u4e00" <= char <= "\u9fff" for char in content
    )
    results[-1]["check_scope"] = "Chinese response and requested terms; not a factual benchmark"

    tool = {
        "type": "function",
        "function": {
            "name": "search_scat_flights",
            "description": "按批次和呼号查询航班。实际航班数据必须调用此工具获取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "batch": {"type": "string"},
                    "callsign": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["batch", "callsign", "limit"],
                "additionalProperties": False,
            },
        },
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "你是航空数据助手。实际数据只能来自工具。需要查询时调用工具，禁止猜测。",
        },
        {
            "role": "user",
            "content": "查询批次scat20161015_20161021中呼号SAS123的航班，最多返回5条。",
        },
    ]
    expected = {"batch": "scat20161015_20161021", "callsign": "SAS123", "limit": 5}
    auto = request("automatic_tool_selection", messages, tools=[tool], tool_choice="auto")

    def valid_call(message: dict[str, Any]) -> bool:
        try:
            calls = message["tool_calls"]
            return (
                len(calls) == 1
                and calls[0]["function"]["name"] == "search_scat_flights"
                and json.loads(calls[0]["function"]["arguments"]) == expected
            )
        except (KeyError, TypeError, ValueError):
            return False

    results[-1]["passed"] = valid_call(auto)
    forced = request(
        "required_tool_selection",
        messages,
        tools=[tool],
        tool_choice="required",
    )
    results[-1]["passed"] = valid_call(forced)

    if valid_call(auto):
        simulated_result = {
            "status": "ok",
            "synthetic_test_data": True,
            "data": {"total": 2, "external_ids": ["TEST-001", "TEST-002"]},
        }
        followup = request(
            "synthetic_tool_result_followup",
            [
                *messages,
                auto,
                {
                    "role": "tool",
                    "tool_call_id": auto["tool_calls"][0]["id"],
                    "content": json.dumps(simulated_result),
                },
                {
                    "role": "user",
                    "content": (
                        "请根据刚才的工具结果回答：共有多少条记录？"
                        "逐个列出external_id，并说明这些是否真实业务数据。"
                    ),
                },
            ],
            tools=[tool],
            tool_choice="none",
        )
        text = str(followup.get("content", ""))
        results[-1]["passed"] = (
            all(value in text for value in ["TEST-001", "TEST-002"])
            and ("2" in text or "两" in text)
            and any(value in text for value in ["模拟", "测试", "合成"])
        )
        results[-1]["check_scope"] = "Synthetic result round-trip; no business tool executed"

    return {
        "model": model,
        "base_url": base_url,
        "business_database_accessed": False,
        "all_checks_passed": all(item["passed"] for item in results),
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8093/v1")
    parser.add_argument("--model", default="scat-qwen-local")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = probe(args.base_url, args.model, args.timeout)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
