"""Structured, privacy-safe operational telemetry for the service.

The module deliberately records metadata only. Request bodies, MongoDB commands,
tool arguments/results, credentials and model prompts are never included.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from pymongo import monitoring

_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "message",
}
_SECRET_KEY_PATTERN = re.compile(
    r"^(api[_-]?key|authorization|cookie|password|passwd|secret|token|access[_-]?token|"
    r"refresh[_-]?token|lease[_-]?token|connection[_-]?string|mongo[_-]?uri|database[_-]?uri|uri)$",
    re.IGNORECASE,
)
_CREDENTIAL_URI_PATTERN = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[=:]\s*([^\s,;]+)"
)
_request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id(default: str = "unknown") -> str:
    return _request_id_context.get() or default


def set_request_id(request_id: str) -> Token[str | None]:
    return _request_id_context.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id_context.reset(token)


def redact_text(value: object) -> str:
    """Remove common credential forms from a value before it reaches a sink."""
    text = str(value)
    text = _CREDENTIAL_URI_PATTERN.sub(r"\1***:***@", text)
    text = _BEARER_PATTERN.sub("Bearer ***", text)
    return _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=***", text)


def _safe_value(key: str, value: Any) -> Any:
    if _SECRET_KEY_PATTERN.search(key):
        return "***"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(key, item) for item in value]
    return redact_text(value)


class JsonLogFormatter(logging.Formatter):
    """One-line JSON formatter that preserves structured ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD_LOG_RECORD_FIELDS:
                continue
            payload[key] = _safe_value(key, value)
        if record.exc_info:
            payload["exception_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] is not None else "Exception"
            )
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_structured_logging(
    *,
    level: int,
    json_logs: bool = True,
    replace_existing_formatters: bool = False,
) -> None:
    """Configure the process root logger without duplicating handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    formatter: logging.Formatter
    if json_logs:
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    owned_handlers = [
        handler for handler in root.handlers if getattr(handler, "_scat_observability", False)
    ]
    if not root.handlers:
        new_handler = logging.StreamHandler()
        new_handler._scat_observability = True  # type: ignore[attr-defined]
        new_handler.setFormatter(formatter)
        root.addHandler(new_handler)
    else:
        handlers_to_update = root.handlers if replace_existing_formatters else owned_handlers
        for existing_handler in handlers_to_update:
            existing_handler.setFormatter(formatter)
    for logger_name in (
        "service.access",
        "service.mongodb",
        "service.agent",
        "scat.materialization.task",
        "scat.experiment.task",
    ):
        logging.getLogger(logger_name).setLevel(logging.INFO)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit a stable event name with request correlation and safe metadata."""
    safe_fields = {key: _safe_value(key, value) for key, value in fields.items()}
    request_id = get_request_id(default="background")
    summary_parts = [f"request_id={request_id}"]
    summary_parts.extend(
        f"{key}={value}"
        for key, value in safe_fields.items()
        if value is None or isinstance(value, (bool, int, float, str))
    )
    logger.log(
        level,
        f"{event} {' '.join(summary_parts)}",
        extra={"event": event, "request_id": request_id, **safe_fields},
    )


def log_alert(logger: logging.Logger, alert_type: str, **fields: Any) -> None:
    """Emit an alert-shaped event for journald/log-collector routing."""
    log_event(
        logger,
        logging.ERROR,
        "operational_alert",
        alert_type=alert_type,
        alert=True,
        **fields,
    )


class MongoCommandObserver(monitoring.CommandListener):
    """PyMongo listener that logs timings/failures without command payloads."""

    def __init__(self, *, slow_ms: float) -> None:
        self.slow_ms = slow_ms
        self.logger = logging.getLogger("service.mongodb")

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        # No-op by design: a completed/failed event contains all safe metadata needed.
        del event

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        duration_ms = event.duration_micros / 1000
        level = logging.WARNING if duration_ms >= self.slow_ms else logging.INFO
        log_event(
            self.logger,
            level,
            "mongodb_query_completed",
            operation=event.command_name,
            database=event.database_name,
            duration_ms=round(duration_ms, 3),
            slow=duration_ms >= self.slow_ms,
        )
        if duration_ms >= self.slow_ms:
            log_alert(
                self.logger,
                "slow_mongodb_query",
                operation=event.command_name,
                database=event.database_name,
                duration_ms=round(duration_ms, 3),
                threshold_ms=self.slow_ms,
            )

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        duration_ms = event.duration_micros / 1000
        failure = event.failure
        failure_type = (
            str(failure.get("codeName") or failure.get("code") or "MongoCommandError")
            if isinstance(failure, Mapping)
            else type(failure).__name__
        )
        log_event(
            self.logger,
            logging.ERROR,
            "mongodb_query_failed",
            operation=event.command_name,
            database=event.database_name,
            duration_ms=round(duration_ms, 3),
            failure_type=failure_type,
        )
        log_alert(
            self.logger,
            "mongodb_query_failure",
            operation=event.command_name,
            database=event.database_name,
            duration_ms=round(duration_ms, 3),
            failure_type=failure_type,
        )


def _usage_from_response(response: Any) -> tuple[int | None, int | None, int | None]:
    input_tokens = output_tokens = total_tokens = None
    llm_output = getattr(response, "llm_output", None) or {}
    raw_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    input_tokens = raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens")
    output_tokens = raw_usage.get("completion_tokens") or raw_usage.get("output_tokens")
    total_tokens = raw_usage.get("total_tokens")
    if input_tokens is None or output_tokens is None:
        for generation_group in getattr(response, "generations", []) or []:
            for generation in generation_group:
                metadata = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if metadata:
                    input_tokens = input_tokens or metadata.get("input_tokens")
                    output_tokens = output_tokens or metadata.get("output_tokens")
                    total_tokens = total_tokens or metadata.get("total_tokens")
                    break
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)
    return input_tokens, output_tokens, total_tokens


class AgentObservabilityCallback(BaseCallbackHandler):
    """Metadata-only LangChain callback for model and tool execution."""

    def __init__(self, *, agent_id: str, model_name: str | None) -> None:
        self.agent_id = agent_id
        self.model_name = model_name or "default"
        self.logger = logging.getLogger("service.agent")
        self._model_started: dict[UUID, float] = {}
        self._tool_started: dict[UUID, tuple[float, str]] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        self._model_started[run_id] = perf_counter()

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        started = self._model_started.pop(run_id, None)
        duration_ms = (perf_counter() - started) * 1000 if started is not None else None
        input_tokens, output_tokens, total_tokens = _usage_from_response(response)
        log_event(
            self.logger,
            logging.INFO,
            "agent_model_completed",
            agent_id=self.agent_id,
            model=self.model_name,
            duration_ms=round(duration_ms, 3) if duration_ms is not None else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        started = self._model_started.pop(run_id, None)
        duration_ms = (perf_counter() - started) * 1000 if started is not None else None
        log_event(
            self.logger,
            logging.ERROR,
            "agent_model_failed",
            agent_id=self.agent_id,
            model=self.model_name,
            duration_ms=round(duration_ms, 3) if duration_ms is not None else None,
            failure_type=type(error).__name__,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del input_str, kwargs
        tool_name = str(serialized.get("name") or serialized.get("id") or "unknown")
        self._tool_started[run_id] = (perf_counter(), tool_name)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del output, kwargs
        started, tool_name = self._tool_started.pop(run_id, (perf_counter(), "unknown"))
        log_event(
            self.logger,
            logging.INFO,
            "agent_tool_completed",
            agent_id=self.agent_id,
            tool=tool_name,
            duration_ms=round((perf_counter() - started) * 1000, 3),
        )

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        started, tool_name = self._tool_started.pop(run_id, (perf_counter(), "unknown"))
        log_event(
            self.logger,
            logging.ERROR,
            "agent_tool_failed",
            agent_id=self.agent_id,
            tool=tool_name,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            failure_type=type(error).__name__,
        )
