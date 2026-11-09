"""FastAPI 服务层：把 HTTP/SSE 请求接到同一组 LangGraph Agent。

本模块负责生命周期资源、运行配置、thread 恢复、流式协议和历史读取；业务
查询与模型执行仍由 Agent 工具后的 Service/Repository 完成。
"""

import asyncio
import inspect
import json
import logging
import warnings
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import (
    CallbackHandler,  # type: ignore[import-untyped]
)
from langgraph.types import Command, Interrupt
from langsmith import Client as LangsmithClient
from langsmith import uuid7

from agents import (
    DEFAULT_AGENT,
    AgentGraph,
    get_all_agent_info,
    load_agent,
)
from agents import (
    get_public_agent as get_agent,
)
from analysis_runs import AnalysisRunConflictError, analysis_run_executor, analysis_run_repository
from core import settings
from core.observability import (
    AgentObservabilityCallback,
    configure_structured_logging,
    log_event,
)
from core.redis_request_limiter import redis_request_limiter
from core.redis_runtime import redis_runtime
from memory import initialize_database, initialize_store
from scat.infrastructure.database import flight_database
from scat_model_runtime import ModelRunStatus
from scat_model_runtime.production import (
    configure_production_model_runtime,
    production_model_runtime_available,
    shutdown_production_model_runtime,
)
from schema import (
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    FeedbackResponse,
    ServiceMetadata,
    StreamInput,
    UserInput,
)
from service.agui import router as agui_router
from service.analysis_api import router as analysis_router
from service.auth import verify_bearer
from service.errors import install_exception_handlers, sse_error_event
from service.experiments_api import router as experiments_router
from service.materialization_api import router as materialization_router
from service.ownership import (
    TrustedIdentityMiddleware,
    authorize_request_run,
    authorize_thread,
    current_owner,
    register_request_run,
)
from service.request_context import RequestContextMiddleware
from service.request_limits import RequestLimitsMiddleware
from service.runtime_validation import validate_runtime_configuration
from service.scat_api import router as scat_router
from service.uav_api import router as uav_router
from service.utils import (
    collect_analysis_results,
    convert_message_content_to_string,
    langchain_to_chat_message,
    remove_tool_calls,
)

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger(__name__)


def custom_generate_unique_id(route: APIRoute) -> str:
    """Generate idiomatic operation IDs for OpenAPI client generation."""
    return route.name


def _get_registered_agent(agent_id: str) -> AgentGraph:
    try:
        return get_agent(agent_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_id}' was not found",
        ) from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Configurable lifespan that initializes the appropriate database checkpointer, store,
    and agents with async loading - for example for starting up MCP clients.
    """
    configure_structured_logging(
        level=settings.LOG_LEVEL.to_logging_level(),
        json_logs=settings.OBSERVABILITY_JSON_LOGS,
        replace_existing_formatters=True,
    )
    app.state.agent_load_status = {}
    app.state.memory_status = "not_initialized"
    app.state.configuration_status = "checking"
    app.state.model_runtime_status = "disabled"

    try:
        agents = get_all_agent_info()
        try:
            validate_runtime_configuration(
                settings,
                default_agent=DEFAULT_AGENT,
                agent_keys=(agent.key for agent in agents),
            )
            app.state.configuration_status = "valid"
        except Exception:
            app.state.configuration_status = "invalid"
            raise

        try:
            await flight_database.connect()
            logger.info("SCAT MongoDB connected")
        except Exception:
            # Keep the API process alive in a degraded state. Readiness will
            # report the database failure and later requests can reconnect.
            logger.exception("SCAT MongoDB was unavailable during startup")

        if await asyncio.to_thread(configure_production_model_runtime, settings):
            app.state.model_runtime_status = (
                "registered" if production_model_runtime_available() else "unavailable"
            )
            logger.info("Production task-model runtime registered")

        # Checkpointer 保存 thread 级图状态；Store 保存跨 thread 的长期信息。
        # 两者在应用生命周期内统一创建和回收，避免每个请求重复建立连接池。
        async with initialize_database() as saver, initialize_store() as store:
            # Set up both components
            if hasattr(saver, "setup"):  # ignore: union-attr
                await saver.setup()
            # Only setup store for Postgres as InMemoryStore doesn't need setup
            if hasattr(store, "setup"):  # ignore: union-attr
                await store.setup()
            app.state.memory_status = "initialized"

            if not settings.AUTH_SECRET:
                logger.warning(
                    "AUTH_SECRET is not configured — all API endpoints are unauthenticated. "
                    "Set AUTH_SECRET in your environment to enable bearer token authentication."
                )

            # Configure agents with both memory components and async loading
            for a in agents:
                try:
                    await load_agent(a.key)
                    app.state.agent_load_status[a.key] = "loaded"
                    logger.info(f"Agent loaded: {a.key}")
                except Exception:
                    app.state.agent_load_status[a.key] = "failed"
                    logger.exception("Failed to load agent %s", a.key)
                    # Continue with other agents rather than failing startup

                agent = get_agent(a.key)
                # 图定义在导入时已经 compile；这里注入具体 saver/store，使同一
                # Agent 可在 SQLite、Postgres 或 Mongo checkpoint 后端上运行。
                agent.checkpointer = saver
                agent.store = store
            yield
    except Exception:
        logger.exception("Error during database/store/agents initialization")
        raise
    finally:
        app.state.memory_status = "stopped"
        try:
            interrupted_run_ids = await analysis_run_executor.shutdown()
            for interrupted_run_id in interrupted_run_ids:
                try:
                    await analysis_run_repository.transition(
                        interrupted_run_id,
                        expected_statuses=(ModelRunStatus.QUEUED, ModelRunStatus.RUNNING),
                        status=ModelRunStatus.CANCELLED,
                        progress=1.0,
                        message="服务停止，异步模型任务已取消。",
                    )
                except AnalysisRunConflictError:
                    logger.info(
                        "Analysis run reached a terminal state during shutdown: %s",
                        interrupted_run_id,
                    )
            if interrupted_run_ids:
                logger.info("Cancelled %d asynchronous model runs", len(interrupted_run_ids))
        except Exception:
            logger.exception("Failed to stop asynchronous model runs cleanly")
        try:
            shutdown_production_model_runtime()
            logger.info("Production task-model runtime stopped")
        except Exception:
            logger.exception("Failed to stop production task-model runtime")
        try:
            await flight_database.close()
            logger.info("SCAT MongoDB connection closed")
        except Exception:
            logger.exception("Failed to close SCAT MongoDB connection")
        try:
            await redis_runtime.close()
            logger.info("Optional Redis connection closed")
        except Exception:
            logger.exception("Failed to close optional Redis connection")


app = FastAPI(lifespan=lifespan, generate_unique_id_function=custom_generate_unique_id)
app.add_middleware(
    RequestLimitsMiddleware,
    max_body_bytes=settings.REQUEST_MAX_BODY_BYTES,
    max_concurrency=settings.REQUEST_MAX_CONCURRENCY,
    global_limiter=redis_request_limiter,
)
app.add_middleware(
    RequestContextMiddleware,
    slow_request_ms=settings.OBSERVABILITY_SLOW_REQUEST_MS,
    error_rate_min_requests=settings.OBSERVABILITY_ERROR_RATE_MIN_REQUESTS,
    error_rate_threshold=settings.OBSERVABILITY_ERROR_RATE_THRESHOLD,
    error_rate_window_seconds=settings.OBSERVABILITY_ERROR_RATE_WINDOW_SECONDS,
    alert_cooldown_seconds=settings.OBSERVABILITY_ALERT_COOLDOWN_SECONDS,
)
install_exception_handlers(app)
router = APIRouter(dependencies=[Depends(verify_bearer)])
# AG-UI protocol endpoints inherit the same bearer auth - see service/agui.py
router.include_router(agui_router)
router.include_router(analysis_router)
router.include_router(experiments_router)
router.include_router(materialization_router)
router.include_router(scat_router)
router.include_router(uav_router)


@router.get("/info")
async def info() -> ServiceMetadata:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return ServiceMetadata(
        agents=get_all_agent_info(),
        models=models,
        default_agent=DEFAULT_AGENT,
        default_model=settings.DEFAULT_MODEL,
    )


async def _handle_input(
    user_input: UserInput,
    agent: AgentGraph,
    *,
    agent_id: str = DEFAULT_AGENT,
) -> tuple[dict[str, Any], UUID]:
    """构造图调用参数，并判断本轮是新输入还是 interrupt 恢复。

    ``thread_id`` 定位 checkpoint，``user_id`` 表示身份/长期作用域；两者职责
    不同。返回值可以直接传给 ``ainvoke`` 或 ``astream``，保证普通与流式接口
    使用完全相同的恢复语义。
    """
    run_id = uuid7()
    thread_id = user_input.thread_id or str(uuid4())
    await authorize_thread(thread_id, agent, create=True)
    # Keep the implicit user scope stable across turns in the same thread.
    # A fresh random value on every request would make thread-bound confirmation
    # handles impossible to validate when API clients omit user_id.
    user_id = current_owner() or user_input.user_id or f"thread:{thread_id}"
    await register_request_run(str(run_id))

    # configurable 会传到节点和工具；确认码据此绑定 thread + user，防止跨
    # 会话或跨用户重放。
    configurable = {"thread_id": thread_id, "user_id": user_id}
    if user_input.model is not None:
        configurable["model"] = user_input.model

    selected_model = user_input.model or settings.DEFAULT_MODEL
    callbacks: list[Any] = [
        AgentObservabilityCallback(
            agent_id=agent_id,
            model_name=str(selected_model) if selected_model is not None else None,
        )
    ]
    if settings.LANGFUSE_TRACING:
        # Initialize Langfuse CallbackHandler for Langchain (tracing)
        langfuse_handler = CallbackHandler()

        callbacks.append(langfuse_handler)

    if user_input.agent_config:
        # Check for reserved keys (including 'model' even if not in configurable)
        reserved_keys = {"thread_id", "user_id", "model"}
        if overlap := reserved_keys & user_input.agent_config.keys():
            raise HTTPException(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        configurable.update(user_input.agent_config)

    config = RunnableConfig(
        configurable=configurable,
        run_id=run_id,
        callbacks=callbacks,
    )

    # 先读取同一 thread 的最新快照。存在 interrupt 时，用户文本不是新的
    # HumanMessage，而是上次 interrupt() 的返回值。
    state = await agent.aget_state(config=config)
    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any]
    if interrupted_tasks:
        # 恢复后包含 interrupt 的节点会从函数开头重跑；节点中的副作用必须
        # 位于 interrupt 之后或具备幂等保护。
        input = Command(resume=user_input.message)
    else:
        input = {"messages": [HumanMessage(content=user_input.message)]}

    kwargs = {
        "input": input,
        "config": config,
    }

    return kwargs, run_id


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(user_input: UserInput, agent_id: str = DEFAULT_AGENT) -> ChatMessage:
    """
    Invoke an agent with user input to retrieve a final response.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.
    """
    # NOTE: Currently this only returns the last message or interrupt.
    # In the case of an agent outputting multiple AIMessages (such as the background step
    # in interrupt-agent, or a tool step in research-assistant), it's omitted. Arguably,
    # you'd want to include it. You could update the API to return a list of ChatMessages
    # in that case.
    agent = _get_registered_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id=agent_id)
    started_at = perf_counter()

    try:
        response_events: list[tuple[str, Any]] = await agent.ainvoke(**kwargs, stream_mode=["updates", "values"])  # type: ignore # fmt: skip
        response_type, response = response_events[-1]
        if response_type == "values":
            # Normal response, the agent completed successfully
            response_messages = response["messages"]
            output = langchain_to_chat_message(response_messages[-1])
            latest_input_index = max(
                (
                    index
                    for index, message in enumerate(response_messages)
                    if isinstance(message, HumanMessage) and message.content == user_input.message
                ),
                default=-1,
            )
            output.analysis_results = collect_analysis_results(
                response_messages[latest_input_index + 1 :]
            )
        elif response_type == "updates" and "__interrupt__" in response:
            # The last thing to occur was an interrupt
            # Return the value of the first interrupt as an AIMessage
            output = langchain_to_chat_message(
                AIMessage(content=response["__interrupt__"][0].value)
            )
        else:
            raise ValueError(f"Unexpected response type: {response_type}")

        output.run_id = str(run_id)
        log_event(
            logger,
            logging.INFO,
            "agent_request_completed",
            agent_id=agent_id,
            run_id=str(run_id),
            model=str(user_input.model or settings.DEFAULT_MODEL or "default"),
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            transport="invoke",
        )
        return output
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "agent_request_failed",
            agent_id=agent_id,
            run_id=str(run_id),
            model=str(user_input.model or settings.DEFAULT_MODEL or "default"),
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            transport="invoke",
            failure_type=type(exc).__name__,
        )
        logger.exception("Agent invocation failed request_id=%s", str(run_id))
        raise HTTPException(status_code=500, detail="Unexpected error") from exc


async def message_generator(
    user_input: StreamInput, agent_id: str = DEFAULT_AGENT
) -> AsyncGenerator[str, None]:
    """
    Generate a stream of messages from the agent.

    This is the workhorse method for the /stream endpoint.
    """
    agent = _get_registered_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id=agent_id)
    started_at = perf_counter()
    succeeded = True

    try:
        # Process streamed events from the graph and yield messages over the SSE stream.
        async for stream_event in agent.astream(
            **kwargs, stream_mode=["updates", "messages", "custom"], subgraphs=True
        ):
            if not isinstance(stream_event, tuple):
                continue
            # Handle different stream event structures based on subgraphs
            if len(stream_event) == 3:
                # With subgraphs=True: (node_path, stream_mode, event)
                _, stream_mode, event = stream_event
            else:
                # Without subgraphs: (stream_mode, event)
                stream_mode, event = stream_event
            new_messages = []
            if stream_mode == "updates":
                for node, updates in event.items():
                    # A simple approach to handle agent interrupts.
                    # In a more sophisticated implementation, we could add
                    # some structured ChatMessage type to return the interrupt value.
                    if node == "__interrupt__":
                        interrupt: Interrupt
                        for interrupt in updates:
                            new_messages.append(AIMessage(content=interrupt.value))
                        continue
                    updates = updates or {}
                    update_messages = updates.get("messages", [])
                    # special cases for using langgraph-supervisor library
                    if "supervisor" in node or "sub-agent" in node:
                        # the only tools that come from the actual agent are the handoff and handback tools
                        if isinstance(update_messages[-1], ToolMessage):
                            if "sub-agent" in node and len(update_messages) > 1:
                                # If this is a sub-agent, we want to keep the last 2 messages - the handback tool, and it's result
                                update_messages = update_messages[-2:]
                            else:
                                # If this is a supervisor, we want to keep the last message only - the handoff result. The tool comes from the 'agent' node.
                                update_messages = [update_messages[-1]]
                        else:
                            update_messages = []
                    new_messages.extend(update_messages)

            if stream_mode == "custom":
                new_messages = [event]

            # LangGraph streaming may emit tuples: (field_name, field_value)
            # e.g. ('content', <str>), ('tool_calls', [ToolCall,...]), ('additional_kwargs', {...}), etc.
            # We accumulate only supported fields into `parts` and skip unsupported metadata.
            # More info at: https://langchain-ai.github.io/langgraph/cloud/how-tos/stream_messages/
            processed_messages = []
            current_message: dict[str, Any] = {}
            for message in new_messages:
                if isinstance(message, tuple):
                    key, value = message
                    # Store parts in temporary dict
                    current_message[key] = value
                else:
                    # Add complete message if we have one in progress
                    if current_message:
                        processed_messages.append(_create_ai_message(current_message))
                        current_message = {}
                    processed_messages.append(message)

            # Add any remaining message parts
            if current_message:
                processed_messages.append(_create_ai_message(current_message))

            for message in processed_messages:
                try:
                    chat_message = langchain_to_chat_message(message)
                    chat_message.run_id = str(run_id)
                except Exception:
                    logger.exception("Failed to parse streamed Agent message")
                    yield f"data: {json.dumps(sse_error_event('Unexpected error'))}\n\n"
                    continue
                # LangGraph re-sends the input message, which feels weird, so drop it
                if chat_message.type == "human" and chat_message.content == user_input.message:
                    continue
                yield f"data: {json.dumps({'type': 'message', 'content': chat_message.model_dump()})}\n\n"

            if stream_mode == "messages":
                if not user_input.stream_tokens:
                    continue
                msg, metadata = event
                if "skip_stream" in metadata.get("tags", []):
                    continue
                # For some reason, astream("messages") causes non-LLM nodes to send extra messages.
                # Drop them.
                if not isinstance(msg, AIMessageChunk):
                    continue
                content = remove_tool_calls(msg.content)
                if content:
                    # Empty content in the context of OpenAI usually means
                    # that the model is asking for a tool to be invoked.
                    # So we only print non-empty content.
                    yield f"data: {json.dumps({'type': 'token', 'content': convert_message_content_to_string(content)})}\n\n"
    except Exception:
        succeeded = False
        logger.exception("Agent message generator failed")
        yield f"data: {json.dumps(sse_error_event('Internal server error'))}\n\n"
    finally:
        log_event(
            logger,
            logging.INFO if succeeded else logging.ERROR,
            "agent_request_completed" if succeeded else "agent_request_failed",
            agent_id=agent_id,
            run_id=str(run_id),
            model=str(user_input.model or settings.DEFAULT_MODEL or "default"),
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            transport="stream",
        )
        yield "data: [DONE]\n\n"


def _create_ai_message(parts: dict) -> AIMessage:
    sig = inspect.signature(AIMessage)
    valid_keys = set(sig.parameters)
    filtered = {k: v for k, v in parts.items() if k in valid_keys}
    return AIMessage(**filtered)


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: {'type': 'token', 'content': ' World'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(user_input: StreamInput, agent_id: str = DEFAULT_AGENT) -> StreamingResponse:
    """
    Stream an agent's response to a user input, including intermediate messages and tokens.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to all messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.

    Set `stream_tokens=false` to return intermediate messages but not token-by-token.
    """
    # SSE 响应头发出后 HTTP 状态已固定为 200，所以先校验 Agent；建连后的
    # 运行错误只能编码为结构化 error event，而不能再改成 HTTP 4xx/5xx。
    graph = _get_registered_agent(agent_id)
    if current_owner() is not None:
        user_input.thread_id = user_input.thread_id or str(uuid4())
        await authorize_thread(user_input.thread_id, graph, create=True)
    return StreamingResponse(
        message_generator(user_input, agent_id),
        media_type="text/event-stream",
    )


@router.post("/feedback")
async def feedback(feedback: Feedback) -> FeedbackResponse:
    """
    Record feedback for a run to LangSmith.

    This is a simple wrapper for the LangSmith create_feedback API, so the
    credentials can be stored and managed in the service rather than the client.
    See: https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
    """
    await authorize_request_run(str(feedback.run_id))
    client = LangsmithClient()
    kwargs = feedback.kwargs or {}
    client.create_feedback(
        run_id=feedback.run_id,
        key=feedback.key,
        score=feedback.score,
        **kwargs,
    )
    return FeedbackResponse()


@router.post("/{agent_id}/history", operation_id="history_with_agent_id")
@router.post("/history")
async def history(input: ChatHistoryInput, agent_id: str = DEFAULT_AGENT) -> ChatHistory:
    """
    Get chat history for a thread and agent.

    If agent_id is not provided, the default agent will be used.
    """
    # history 读取的是该 Agent checkpointer 中指定 thread 的 StateSnapshot，
    # 不是从业务 MongoDB 拼装聊天记录。
    agent = _get_registered_agent(agent_id)
    await authorize_thread(input.thread_id, agent, create=False)
    try:
        state_snapshot = await agent.aget_state(
            config=RunnableConfig(configurable={"thread_id": input.thread_id})
        )
        messages: list[AnyMessage] = state_snapshot.values["messages"]
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return ChatHistory(messages=chat_messages)
    except Exception as exc:
        logger.exception("Failed to load Agent chat history")
        raise HTTPException(status_code=500, detail="Unexpected error") from exc


async def _scat_database_status() -> str:
    """Return a safe dependency status without exposing connection details."""
    try:
        await flight_database.ping()
    except Exception:
        # Readiness probes may run every few seconds while MongoDB is down;
        # keep the expected failure at debug level to avoid log flooding.
        logger.debug("SCAT MongoDB health check failed", exc_info=True)
        return "disconnected"
    return "connected"


async def _redis_status() -> str:
    return await redis_runtime.status()


def _agent_health_status() -> dict[str, str]:
    return dict(getattr(app.state, "agent_load_status", {}))


@app.get("/health/live")
async def liveness_check() -> dict[str, str]:
    """Report whether the API process is alive without checking dependencies."""
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness_check(response: Response) -> dict[str, Any]:
    """Report whether critical dependencies for the default Agent are ready."""
    database_status = await _scat_database_status()
    agent_statuses = _agent_health_status()
    default_agent_status = agent_statuses.get(DEFAULT_AGENT, "unknown")
    memory_status = getattr(app.state, "memory_status", "unknown")
    configuration_status = getattr(app.state, "configuration_status", "unknown")
    model_runtime_status = getattr(app.state, "model_runtime_status", "unknown")
    model_runtime_ready = (
        model_runtime_status == "registered"
        if settings.SCAT_GBAR_V23_ENABLED
        else model_runtime_status == "disabled"
    )

    ready = (
        database_status == "connected"
        and default_agent_status == "loaded"
        and memory_status == "initialized"
        and configuration_status == "valid"
        and model_runtime_ready
    )
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if ready else "not_ready",
        "checks": {
            "scat_mongodb": database_status,
            "memory": memory_status,
            "configuration": configuration_status,
            "model_runtime": {
                "enabled": settings.SCAT_GBAR_V23_ENABLED,
                "status": model_runtime_status,
            },
            "default_agent": {
                "name": DEFAULT_AGENT,
                "status": default_agent_status,
            },
        },
    }


@app.get("/health/dependencies")
async def dependency_health_check() -> dict[str, Any]:
    """Return diagnostic status for critical and optional dependencies."""
    database_status = await _scat_database_status()
    agent_statuses = _agent_health_status()
    default_agent_status = agent_statuses.get(DEFAULT_AGENT, "unknown")
    memory_status = getattr(app.state, "memory_status", "unknown")
    configuration_status = getattr(app.state, "configuration_status", "unknown")
    model_runtime_status = getattr(app.state, "model_runtime_status", "unknown")
    redis_status = await _redis_status()
    model_runtime_ready = (
        model_runtime_status == "registered"
        if settings.SCAT_GBAR_V23_ENABLED
        else model_runtime_status == "disabled"
    )

    critical_ok = (
        database_status == "connected"
        and default_agent_status == "loaded"
        and memory_status == "initialized"
        and configuration_status == "valid"
        and model_runtime_ready
    )

    return {
        "status": "ok" if critical_ok else "degraded",
        "dependencies": {
            "scat_mongodb": {
                "status": database_status,
                "critical": True,
            },
            "memory": {
                "status": memory_status,
                "critical": True,
            },
            "configuration": {
                "status": configuration_status,
                "critical": True,
            },
            "model_runtime": {
                "status": model_runtime_status,
                "critical": settings.SCAT_GBAR_V23_ENABLED,
                "enabled": settings.SCAT_GBAR_V23_ENABLED,
            },
            "agents": {
                "status": "loaded" if default_agent_status == "loaded" else "degraded",
                "critical": True,
                "default": DEFAULT_AGENT,
                "items": agent_statuses,
            },
            "langfuse": {
                "status": "configured" if settings.LANGFUSE_TRACING else "disabled",
                "critical": False,
            },
            "redis": {
                "status": redis_status,
                "critical": False,
                "enabled": redis_runtime.enabled,
            },
        },
    }


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Backward-compatible alias for the lightweight liveness check."""
    return await liveness_check()


app.include_router(router)
app.add_middleware(TrustedIdentityMiddleware)
