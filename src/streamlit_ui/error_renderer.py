"""Safe, actionable Streamlit error presentation for client failures."""

from __future__ import annotations

import re
from dataclasses import dataclass

import streamlit as st

from client import AgentClientError


_REQUEST_ID_PATTERN = re.compile(r"request_id[=:]([A-Za-z0-9._:-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ErrorPresentation:
    title: str
    message: str
    action: str
    request_id: str | None = None


def describe_client_error(error: AgentClientError) -> ErrorPresentation:
    """Classify a safe AgentClientError string without exposing a traceback."""
    raw = str(error).strip() or "未知客户端错误"
    request_match = _REQUEST_ID_PATTERN.search(raw)
    request_id = request_match.group(1) if request_match else None
    message = _REQUEST_ID_PATTERN.sub("", raw).replace("()", "").strip(" :")
    lowered = raw.lower()

    if "timeout" in lowered or "timed out" in lowered or "超时" in raw:
        title = "请求超时"
        action = "请缩小查询范围或稍后重试；不要连续重复提交同一请求。"
    elif "401" in lowered or "unauthorized" in lowered or "认证" in raw:
        title = "认证失败"
        action = "请检查本机 AUTH_SECRET 是否与后端配置一致。"
    elif "no agent selected" in lowered or "model unavailable" in lowered:
        title = "Agent 或模型不可用"
        action = "请在设置中选择可用的 Agent 和模型后重试。"
    elif "404" in lowered or "not found" in lowered or "没有找到" in raw:
        title = "资源不存在"
        action = "请核对 thread_id、数据集和对象标识后重试。"
    elif "422" in lowered or "invalid" in lowered or "参数" in raw:
        title = "请求参数不合法"
        action = "请检查批次、对象标识、时间范围和通道参数。"
    elif "503" in lowered or "unavailable" in lowered or "connect" in lowered:
        title = "Agent 服务暂不可用"
        action = "请确认后端服务与 MongoDB 已启动，然后稍后重试。"
    elif "500" in lowered or "internal" in lowered:
        title = "服务内部错误"
        action = "请保留 request ID，并在服务器日志中按该 ID 排查。"
    else:
        title = "请求失败"
        action = "请核对输入后重试；若问题持续，请记录 request ID。"

    return ErrorPresentation(
        title=title,
        message=message,
        action=action,
        request_id=request_id,
    )


def render_client_error(error: AgentClientError, *, context: str) -> None:
    """Render one consistent error state for connect, history, chat or feedback."""
    presentation = describe_client_error(error)
    st.error(f"{context}失败：{presentation.title}")
    st.write(presentation.message)
    if presentation.request_id:
        st.caption(f"request ID：{presentation.request_id}")
    st.info(presentation.action)


__all__ = ["ErrorPresentation", "describe_client_error", "render_client_error"]
