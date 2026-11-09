"""Feedback widget rendered under the latest assistant message."""

from __future__ import annotations

import streamlit as st

from client import AgentClient, AgentClientError
from streamlit_ui.error_renderer import render_client_error


async def render_feedback_widget() -> None:
    """Draw a stars widget and record feedback for the latest run, deduplicated."""
    if "last_feedback" not in st.session_state:
        st.session_state.last_feedback = (None, None)
    if not st.session_state.messages:
        return

    latest_run_id = st.session_state.messages[-1].run_id
    feedback = st.feedback("stars", key=latest_run_id)

    if feedback is None or (latest_run_id, feedback) == st.session_state.last_feedback:
        return

    # Normalize the feedback value (an index) to a score between 0 and 1.
    normalized_score = (feedback + 1) / 5.0
    agent_client: AgentClient = st.session_state.agent_client
    try:
        await agent_client.acreate_feedback(
            run_id=latest_run_id,
            key="human-feedback-stars",
            score=normalized_score,
            kwargs={"comment": "In-line human feedback"},
        )
    except AgentClientError as exc:
        render_client_error(exc, context="提交反馈")
        st.stop()
    st.session_state.last_feedback = (latest_run_id, feedback)
    st.toast("Feedback recorded", icon=":material/reviews:")


__all__ = ["render_feedback_widget"]
