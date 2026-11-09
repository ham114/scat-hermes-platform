"""Generic presentation rules for structured tool status payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ToolStatusPresentation:
    level: Literal["info", "warning", "error"]
    message: str


def describe_tool_status(content: Any) -> ToolStatusPresentation | None:
    """Return a user-facing state for known tool failures, or None for legacy output."""
    payload = content
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = "工具没有返回更多说明。"

    if status == "not_found":
        return ToolStatusPresentation("info", f"没有找到匹配数据：{message}")
    if status == "invalid_request":
        return ToolStatusPresentation("warning", f"查询参数不合法：{message}")
    if status == "database_error":
        return ToolStatusPresentation("error", "数据库查询失败，请稍后重试。")
    if status == "internal_error":
        return ToolStatusPresentation("error", "工具执行失败，请保留当前会话后排查。")
    return None


def render_tool_status(container: Any, content: Any) -> bool:
    """Render a known tool state and report whether the content was handled."""
    presentation = describe_tool_status(content)
    if presentation is None:
        return False
    getattr(container, presentation.level)(presentation.message)
    return True


__all__ = ["ToolStatusPresentation", "describe_tool_status", "render_tool_status"]
