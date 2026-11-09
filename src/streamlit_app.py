import asyncio
import os
import urllib.parse
import uuid
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import streamlit as st
from dotenv import load_dotenv

from client import AgentClient, AgentClientError
from schema import ChatHistory, ChatMessage
from streamlit_ui.chat import draw_messages
from streamlit_ui.error_renderer import render_client_error
from streamlit_ui.feedback import render_feedback_widget
from streamlit_ui.settings import get_welcome_message, render_settings_popover
from voice import VoiceManager

# A Streamlit app for interacting with the langgraph agent via a simple chat interface.
# The app has two main functions which are both run async:

# - main() - sets up the streamlit app and high level structure
# - draw_messages() - (in streamlit_ui/chat.py) draws chat messages, replaying
#   existing ones or streaming new ones.

# The app heavily uses AgentClient to interact with the agent's FastAPI endpoints.


APP_TITLE = "航空数据智能分析"
APP_ICON = "✈️"
USER_ID_COOKIE = "user_id"
DEFAULT_PORTAL_URL = "http://localhost:5173"
TRUSTED_USER_HEADER = "X-Authenticated-User"


def get_portal_url() -> str:
    """Return one public Vue URL without accepting non-HTTP navigation targets."""

    value = os.getenv("PORTAL_URL", DEFAULT_PORTAL_URL).strip()
    if value == "/":
        return value
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value.rstrip("/") or DEFAULT_PORTAL_URL
    return DEFAULT_PORTAL_URL


def normalize_thread_id(value: str | None) -> str | None:
    """Accept bounded printable thread IDs used by the service history API."""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 200:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None
    return candidate


def normalize_user_id(value: str | None) -> str | None:
    """Normalize an identity without changing its authorization semantics."""

    return normalize_thread_id(value)


def get_or_create_user_id() -> str:
    """Get the user ID from session state or URL parameters, or create a new one if it doesn't exist."""
    # Resolve server-controlled identity before session/URL state so a stale
    # browser session can never override a newly authenticated proxy subject.
    server_user_id = normalize_user_id(st.context.headers.get(TRUSTED_USER_HEADER))
    if os.getenv("ANALYSIS_IDENTITY_MODE") == "trusted_proxy" and server_user_id is None:
        st.error("请通过已登录的统一入口打开工作台。")
        st.stop()
    if server_user_id is None:
        server_user_id = normalize_user_id(os.getenv("ANALYSIS_SINGLE_USER_ID"))
    if server_user_id is not None:
        previous = st.session_state.get(USER_ID_COOKIE)
        if previous is not None and previous != server_user_id:
            for key in list(st.session_state):
                del st.session_state[key]
        st.session_state[USER_ID_COOKIE] = server_user_id
        st.session_state["server_identity"] = True
        if USER_ID_COOKIE in st.query_params:
            del st.query_params[USER_ID_COOKIE]
        return server_user_id

    # Legacy anonymous sessions remain available for chat-only development.
    if USER_ID_COOKIE in st.session_state:
        return st.session_state[USER_ID_COOKIE]

    # Try to get from URL parameters using the new st.query_params
    if USER_ID_COOKIE in st.query_params:
        user_id = st.query_params[USER_ID_COOKIE]
        st.session_state[USER_ID_COOKIE] = user_id
        return user_id

    # Generate a new user_id if not found
    user_id = str(uuid.uuid4())

    # Store in session state for this session
    st.session_state[USER_ID_COOKIE] = user_id

    # Also add to URL parameters so it can be bookmarked/shared
    st.query_params[USER_ID_COOKIE] = user_id

    return user_id


async def main() -> None:
    load_dotenv()
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=APP_ICON,
        menu_items={},
    )

    # Hide the streamlit upper-right chrome
    st.html(
        """
        <style>
        [data-testid="stStatusWidget"] {
                visibility: hidden;
                height: 0%;
                position: fixed;
            }
        </style>
        """,
    )
    if st.get_option("client.toolbarMode") != "minimal":
        st.set_option("client.toolbarMode", "minimal")
        await asyncio.sleep(0.1)
        st.rerun()

    # Get or create user ID
    user_id = get_or_create_user_id()

    if "agent_client" not in st.session_state:
        agent_url = os.getenv("AGENT_URL")
        if not agent_url:
            host = os.getenv("HOST", "0.0.0.0")
            port = os.getenv("PORT", 8080)
            agent_url = f"http://{host}:{port}"
        try:
            with st.spinner("Connecting to agent service..."):
                client_options = {}
                if os.getenv("ANALYSIS_IDENTITY_MODE") == "trusted_proxy":
                    client_options["trusted_user_id"] = user_id
                st.session_state.agent_client = AgentClient(base_url=agent_url, **client_options)
        except AgentClientError as e:
            render_client_error(e, context="连接 Agent 服务")
            st.stop()
    agent_client: AgentClient = st.session_state.agent_client

    # Initialize voice manager (once per session)
    if "voice_manager" not in st.session_state:
        st.session_state.voice_manager = VoiceManager.from_env()
    voice = st.session_state.voice_manager

    if "thread_id" not in st.session_state:
        requested_thread_id = st.query_params.get("thread_id")
        thread_id = normalize_thread_id(requested_thread_id)
        if requested_thread_id and thread_id is None:
            st.warning("链接中的 thread_id 无效，已为你创建新对话。")
        if not thread_id:
            thread_id = str(uuid.uuid4())
            messages = []
        else:
            # Read the agent from the URL so history is fetched through the graph that
            # created the thread.
            resume_agent = st.query_params.get("agent") or agent_client.agent
            try:
                messages: ChatHistory = agent_client.get_history(
                    thread_id=thread_id, agent=resume_agent
                ).messages
            except AgentClientError as exc:
                render_client_error(exc, context="恢复对话历史")
                messages = []
        st.session_state.messages = messages
        st.session_state.thread_id = thread_id

    # Keep thread_id in the URL so the address bar is directly shareable.
    st.query_params["thread_id"] = st.session_state.thread_id

    # Config options
    with st.sidebar:
        st.header(f"{APP_ICON} {APP_TITLE}")

        st.caption("自然语言查询 SCAT 航班与固定翼 UAV 传感器数据")

        st.link_button(
            ":material/home: 返回系统首页",
            get_portal_url(),
            use_container_width=True,
        )

        if st.button(":material/chat: New Chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.thread_id = str(uuid.uuid4())
            # Clear saved audio when starting new chat
            if "last_audio" in st.session_state:
                del st.session_state.last_audio
            st.rerun()

        with st.popover(":material/settings: Settings", use_container_width=True):
            model, use_streaming, enable_audio = render_settings_popover(
                agent_client, user_id, voice
            )

        @st.dialog("Architecture")
        def architecture_dialog() -> None:
            st.image(
                "https://github.com/JoshuaC215/agent-service-toolkit/blob/main/media/agent_architecture.png?raw=true"
            )
            "[View full size on Github](https://github.com/JoshuaC215/agent-service-toolkit/blob/main/media/agent_architecture.png)"
            st.caption(
                "App hosted on [Streamlit Cloud](https://share.streamlit.io/) with FastAPI service running in [Azure](https://learn.microsoft.com/en-us/azure/app-service/)"
            )

        if st.button(":material/schema: Architecture", use_container_width=True):
            architecture_dialog()

        with st.popover(":material/policy: Privacy", use_container_width=True):
            st.write(
                "Prompts, responses and feedback in this app are anonymously recorded and saved to LangSmith for product evaluation and improvement purposes only."
            )

        @st.dialog("Share/resume chat")
        def share_chat_dialog() -> None:
            # st.context.url is the browser URL (with query string stripped). Rebuild
            # the params, including the agent so the thread resumes through the right graph.
            if not st.context.url:
                st.error("Could not determine the app URL to build a shareable link.")
                return
            query_values = {
                "thread_id": st.session_state.thread_id,
                "agent": agent_client.agent,
            }
            if not st.session_state.get("server_identity", False):
                query_values[USER_ID_COOKIE] = user_id
            query = urllib.parse.urlencode(query_values)
            chat_url = f"{st.context.url}?{query}"
            st.markdown(f"**Chat URL:**\n```text\n{chat_url}\n```")
            st.info("Copy the above URL to share or revisit this chat")

        if st.button(":material/upload: Share/resume chat", use_container_width=True):
            share_chat_dialog()

        "[View the source code](https://github.com/JoshuaC215/agent-service-toolkit)"
        st.caption(
            "Made with :material/favorite: by [Joshua](https://www.linkedin.com/in/joshua-k-carroll/) in Oakland"
        )

    # Draw existing messages
    messages: list[ChatMessage] = st.session_state.messages

    if len(messages) == 0:
        with st.chat_message("ai"):
            st.write(get_welcome_message(agent_client.agent))

    # draw_messages() expects an async iterator over messages
    async def amessage_iter() -> AsyncGenerator[ChatMessage, None]:
        for m in messages:
            yield m

    await draw_messages(amessage_iter())

    # Render saved audio for the last AI message (if it exists)
    # This ensures audio persists across st.rerun() calls
    if (
        voice
        and enable_audio
        and "last_audio" in st.session_state
        and st.session_state.last_message
        and len(messages) > 0
        and messages[-1].type == "ai"
    ):
        with st.session_state.last_message:
            audio_data = st.session_state.last_audio
            st.audio(audio_data["data"], format=audio_data["format"])

    # Generate new message if the user provided new input
    # Use voice manager if available, otherwise fall back to regular input
    # REQUIRED: Set VOICE_STT_PROVIDER, VOICE_TTS_PROVIDER, OPENAI_API_KEY
    # in app .env (NOT service .env) to enable voice features.
    if voice:
        user_input = voice.get_chat_input()
    else:
        user_input = st.chat_input("输入统计或查询问题…")

    if user_input:
        messages.append(ChatMessage(type="human", content=user_input))
        st.chat_message("human").write(user_input)
        try:
            if use_streaming:
                stream = agent_client.astream(
                    message=user_input,
                    model=model,
                    thread_id=st.session_state.thread_id,
                    user_id=user_id,
                )
                await draw_messages(
                    stream,
                    is_new=True,
                    message_index_start=len(messages),
                )
                # Generate TTS audio for streaming response
                # Note: draw_messages() stores the final message in st.session_state.messages
                # and the container reference in st.session_state.last_message
                if voice and enable_audio and st.session_state.messages:
                    last_msg = st.session_state.messages[-1]
                    # Only generate audio for AI responses with content
                    if last_msg.type == "ai" and last_msg.content:
                        # Use audio_only=True since text was already streamed by draw_messages()
                        voice.render_message(
                            last_msg.content,
                            container=st.session_state.last_message,
                            audio_only=True,
                        )
            else:
                response = await agent_client.ainvoke(
                    message=user_input,
                    model=model,
                    thread_id=st.session_state.thread_id,
                    user_id=user_id,
                )
                messages.append(response)
                # Render AI response with optional voice
                with st.chat_message("ai"):
                    if voice and enable_audio:
                        voice.render_message(response.content)
                    else:
                        st.write(response.content)
                    for analysis_index, analysis_result in enumerate(
                        response.analysis_results
                    ):
                        from streamlit_ui.analysis_renderer import render_analysis_result

                        render_analysis_result(
                            analysis_result,
                            widget_prefix=(
                                f"m{len(messages) - 1}_final_{analysis_index}"
                            ),
                        )
            st.rerun()  # Clear stale containers
        except AgentClientError as e:
            render_client_error(e, context="生成回答")
            st.stop()

    # If messages have been generated, show feedback widget
    if len(messages) > 0 and st.session_state.last_message:
        with st.session_state.last_message:
            await render_feedback_widget()


if __name__ == "__main__":
    asyncio.run(main())
