"""Sidebar settings helpers for the Streamlit main app."""

from __future__ import annotations

from typing import Any

import streamlit as st

from client import AgentClient


def get_welcome_message(agent_key: str) -> str:
    """Return the dataset-specific welcome text for an empty conversation."""
    messages = {
        "chatbot": "Hello! I'm a simple chatbot. Ask me anything!",
        "interrupt-agent": (
            "Hello! I'm an interrupt agent. Tell me your birthday and I will "
            "predict your personality!"
        ),
        "research-assistant": (
            "Hello! I'm an AI-powered research assistant with web search and a "
            "calculator. Ask me anything!"
        ),
        "rag-assistant": """Hello! I'm an AI-powered Company Policy & HR assistant with access to AcmeTech's Employee Handbook.
            I can help you find information about benefits, remote work, time-off policies, company values, and more. Ask me anything!""",
        "scat-flight-agent": """你好，我可以查询两个航空数据集：

- **SCAT**：航班、批次、轨迹、飞行计划与原始空管预测；
- **UAV**：民用固定翼飞行器架次、机型、传感器通道与统计摘要。

你可以问“SCAT 当前批次的主要机型是什么”，或“UAV 机型 0001 有多少架次”。如未说明数据集，我会先确认统计口径。""",
    }
    return messages.get(agent_key, "Hello! I'm an AI agent. Ask me anything!")


def render_settings_popover(
    agent_client: AgentClient,
    user_id: str,
    voice: Any,
) -> tuple[str | None, bool, bool]:
    """Draw the settings popover content.

    Returns (model, use_streaming, enable_audio). Must be called inside a
    ``st.popover`` container by the caller.
    """
    model_idx = agent_client.info.models.index(agent_client.info.default_model)
    model = st.selectbox(
        "LLM to use", options=agent_client.info.models, index=model_idx
    )
    agent_list = [agent.key for agent in agent_client.info.agents]
    agent_idx = agent_list.index(agent_client.info.default_agent)
    # Sync the selection to the ?agent= URL param (dropped when it's the default).
    agent_client.agent = st.selectbox(
        "Agent to use",
        options=agent_list,
        index=agent_idx,
        key="agent",
        bind="query-params",
    )
    use_streaming = st.toggle("Stream results", value=True)
    # Audio toggle with callback: clears cached audio when toggled off.
    enable_audio = st.toggle(
        "Enable audio generation",
        value=True,
        disabled=not voice or not voice.tts,
        help="Configure VOICE_TTS_PROVIDER in .env to enable"
        if not voice or not voice.tts
        else None,
        on_change=lambda: (
            st.session_state.pop("last_audio", None)
            if not st.session_state.get("enable_audio", True)
            else None
        ),
        key="enable_audio",
    )

    # Display user ID (for debugging or user information).
    st.text_input("User ID (read-only)", value=user_id, disabled=True)
    return model, use_streaming, enable_audio


__all__ = ["get_welcome_message", "render_settings_popover"]
