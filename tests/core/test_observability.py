import json
import logging
from types import SimpleNamespace
from uuid import uuid4

from core.observability import (
    AgentObservabilityCallback,
    JsonLogFormatter,
    MongoCommandObserver,
    log_event,
    redact_text,
    reset_request_id,
    set_request_id,
)


def test_json_formatter_preserves_metrics_and_redacts_credentials() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connected mongodb://user:password@example.test/db token=abc123",
        args=(),
        exc_info=None,
    )
    record.input_tokens = 12
    record.total_tokens = 34
    record.api_key = "super-secret"

    payload = json.loads(formatter.format(record))

    assert "password" not in payload["message"]
    assert "abc123" not in payload["message"]
    assert payload["input_tokens"] == 12
    assert payload["total_tokens"] == 34
    assert payload["api_key"] == "***"


def test_log_event_uses_request_context_without_sensitive_values(caplog) -> None:
    token = set_request_id("trace-123")
    try:
        with caplog.at_level(logging.INFO, logger="test.observability"):
            log_event(
                logging.getLogger("test.observability"),
                logging.INFO,
                "safe_event",
                duration_ms=12.5,
                password="do-not-log",
            )
    finally:
        reset_request_id(token)

    record = caplog.records[-1]
    assert record.request_id == "trace-123"
    assert record.duration_ms == 12.5
    assert record.password == "***"
    assert "do-not-log" not in caplog.text


def test_redact_text_covers_bearer_and_assignment() -> None:
    value = redact_text("Bearer abc.def password=hunter2")
    assert "abc.def" not in value
    assert "hunter2" not in value


def test_mongo_observer_logs_duration_without_command_payload(caplog) -> None:
    observer = MongoCommandObserver(slow_ms=10)
    event = SimpleNamespace(
        duration_micros=25_000,
        command_name="aggregate",
        database_name="flight_data",
    )

    with caplog.at_level(logging.DEBUG, logger="service.mongodb"):
        observer.succeeded(event)

    completed = next(
        record for record in caplog.records if record.event == "mongodb_query_completed"
    )
    assert completed.operation == "aggregate"
    assert completed.duration_ms == 25.0
    assert not hasattr(completed, "command")
    assert any(
        getattr(record, "alert_type", None) == "slow_mongodb_query" for record in caplog.records
    )


def test_agent_callback_logs_tool_metadata_but_not_payload(caplog) -> None:
    callback = AgentObservabilityCallback(agent_id="scat-flight-agent", model_name="fake")
    run_id = uuid4()

    with caplog.at_level(logging.INFO, logger="service.agent"):
        callback.on_tool_start(
            {"name": "get_flight"},
            "secret user payload",
            run_id=run_id,
        )
        callback.on_tool_end("private result", run_id=run_id)

    record = caplog.records[-1]
    assert record.event == "agent_tool_completed"
    assert record.tool == "get_flight"
    assert "secret user payload" not in caplog.text
    assert "private result" not in caplog.text


def test_agent_callback_logs_model_token_usage(caplog) -> None:
    callback = AgentObservabilityCallback(agent_id="scat-flight-agent", model_name="fake")
    run_id = uuid4()
    callback.on_chat_model_start({}, [], run_id=run_id)
    response = SimpleNamespace(
        llm_output={
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }
        },
        generations=[],
    )

    with caplog.at_level(logging.INFO, logger="service.agent"):
        callback.on_llm_end(response, run_id=run_id)

    record = caplog.records[-1]
    assert record.event == "agent_model_completed"
    assert record.model == "fake"
    assert record.input_tokens == 10
    assert record.output_tokens == 5
    assert record.total_tokens == 15
