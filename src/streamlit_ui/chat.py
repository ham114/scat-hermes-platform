"""Chat message rendering and sub-agent orchestration for the Streamlit app."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import streamlit as st
from pydantic import ValidationError

from schema import ChatMessage
from schema.task_data import TaskData, TaskDataStatus
from streamlit_ui.analysis_renderer import render_analysis_result
from streamlit_ui.tool_status import render_tool_status


async def draw_messages(
    messages_agen: AsyncGenerator[ChatMessage | str, None],
    is_new: bool = False,
    message_index_start: int = 0,
) -> None:
    """Draws chat messages - either replaying existing or streaming new ones.

    Streaming tokens render through a placeholder; tool calls render inside
    status containers that are tracked by tool call ID.
    """
    last_message_type = None
    st.session_state.last_message = None

    streaming_content = ""
    streaming_placeholder = None
    message_index = message_index_start

    while msg := await anext(messages_agen, None):
        # str message represents an intermediate token being streamed.
        if isinstance(msg, str):
            if not streaming_placeholder:
                if last_message_type != "ai":
                    last_message_type = "ai"
                    st.session_state.last_message = st.chat_message("ai")
                with st.session_state.last_message:
                    streaming_placeholder = st.empty()

            streaming_content += msg
            streaming_placeholder.write(streaming_content)
            continue
        if not isinstance(msg, ChatMessage):
            st.error(f"Unexpected message type: {type(msg)}")
            st.write(msg)
            st.stop()
        current_index = message_index
        message_index += 1

        match msg.type:
            case "human":
                last_message_type = "human"
                st.chat_message("human").write(msg.content)

            case "ai":
                if is_new:
                    st.session_state.messages.append(msg)

                if last_message_type != "ai":
                    last_message_type = "ai"
                    st.session_state.last_message = st.chat_message("ai")

                with st.session_state.last_message:
                    if msg.content:
                        if streaming_placeholder:
                            streaming_placeholder.write(msg.content)
                            streaming_content = ""
                            streaming_placeholder = None
                        else:
                            st.write(msg.content)

                    for analysis_index, analysis_result in enumerate(
                        msg.analysis_results
                    ):
                        render_analysis_result(
                            analysis_result,
                            widget_prefix=f"m{current_index}_ai_{analysis_index}",
                        )

                    if msg.tool_calls:
                        call_results = {}
                        for tool_call in msg.tool_calls:
                            if "transfer_to" in tool_call["name"]:
                                label = f"""💼 Sub Agent: {tool_call["name"]}"""
                            else:
                                label = f"""🛠️ Tool Call: {tool_call["name"]}"""

                            status = st.status(
                                label,
                                state="running" if is_new else "complete",
                            )
                            call_results[tool_call["id"]] = status

                        for tool_call in msg.tool_calls:
                            if "transfer_to" in tool_call["name"]:
                                status = call_results[tool_call["id"]]
                                status.update(expanded=True)
                                await handle_sub_agent_msgs(
                                    messages_agen, status, is_new
                                )
                                break

                            status = call_results[tool_call["id"]]
                            status.write("Input:")
                            status.write(tool_call["args"])
                            tool_result: ChatMessage = await anext(messages_agen)

                            if tool_result.type != "tool":
                                st.error(
                                    f"Unexpected ChatMessage type: {tool_result.type}"
                                )
                                st.write(tool_result)
                                st.stop()

                            if is_new:
                                st.session_state.messages.append(tool_result)
                            if tool_result.tool_call_id:
                                status = call_results[tool_result.tool_call_id]
                            if tool_result.analysis_results:
                                status.write("已生成可视化结构化结果。")
                            else:
                                handled = render_tool_status(
                                    status, tool_result.content
                                )
                                if not handled:
                                    status.write("Output:")
                                    status.write(tool_result.content)
                            status.update(state="complete")
                            for analysis_index, analysis_result in enumerate(
                                tool_result.analysis_results
                            ):
                                render_analysis_result(
                                    analysis_result,
                                    widget_prefix=(
                                        f"m{current_index}_tool_{analysis_index}"
                                    ),
                                )

            case "custom":
                try:
                    task_data: TaskData = TaskData.model_validate(msg.custom_data)
                except ValidationError:
                    st.error("Unexpected CustomData message received from agent")
                    st.write(msg.custom_data)
                    st.stop()

                if is_new:
                    st.session_state.messages.append(msg)

                if last_message_type != "task":
                    last_message_type = "task"
                    st.session_state.last_message = st.chat_message(
                        name="task", avatar=":material/manufacturing:"
                    )
                    with st.session_state.last_message:
                        status = TaskDataStatus()

                status.add_and_draw_task_data(task_data)

            case _:
                st.error(f"Unexpected ChatMessage type: {msg.type}")
                st.write(msg)
                st.stop()


async def handle_sub_agent_msgs(messages_agen, status, is_new):
    """Segregate sub-agent output into the given status container.

    Handles nested agent hierarchies with handoff-back messages.
    """
    nested_popovers = {}

    first_msg = await anext(messages_agen)
    if is_new:
        st.session_state.messages.append(first_msg)

    while True:
        sub_msg = await anext(messages_agen)

        if is_new:
            st.session_state.messages.append(sub_msg)

        if sub_msg.type == "tool" and sub_msg.tool_call_id in nested_popovers:
            popover = nested_popovers[sub_msg.tool_call_id]
            handled = render_tool_status(popover, sub_msg.content)
            if not handled:
                popover.write("**Output:**")
                popover.write(sub_msg.content)
            continue

        if (
            hasattr(sub_msg, "tool_calls")
            and sub_msg.tool_calls
            and any(
                "transfer_back_to" in tc.get("name", "")
                for tc in sub_msg.tool_calls
            )
        ):
            for tc in sub_msg.tool_calls:
                if "transfer_back_to" in tc.get("name", ""):
                    transfer_result = await anext(messages_agen)
                    if is_new:
                        st.session_state.messages.append(transfer_result)

            if status:
                status.update(state="complete")
            break

        if status:
            if sub_msg.content:
                status.write(sub_msg.content)

            if hasattr(sub_msg, "tool_calls") and sub_msg.tool_calls:
                for tc in sub_msg.tool_calls:
                    if "transfer_to" in tc["name"]:
                        nested_status = status.status(
                            f"""💼 Sub Agent: {tc["name"]}""",
                            state="running" if is_new else "complete",
                            expanded=True,
                        )
                        await handle_sub_agent_msgs(
                            messages_agen, nested_status, is_new
                        )
                    else:
                        popover = status.popover(f"{tc['name']}", icon="🛠️")
                        popover.write(f"**Tool:** {tc['name']}")
                        popover.write("**Input:**")
                        popover.write(tc["args"])
                        nested_popovers[tc["id"]] = popover


__all__ = ["draw_messages", "handle_sub_agent_msgs"]
