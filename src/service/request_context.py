"""Per-request correlation IDs and access logging."""

from __future__ import annotations

import logging
import re
from collections import deque
from time import perf_counter
from typing import Any
from uuid import uuid4

from core.observability import (
    get_request_id,
    log_alert,
    log_event,
    reset_request_id,
    set_request_id,
)

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
logger = logging.getLogger("service.access")
__all__ = [
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "get_request_id",
    "normalize_request_id",
]


def _new_request_id() -> str:
    return str(uuid4())


def normalize_request_id(candidate: str | None) -> str:
    """Accept safe caller IDs or create a server-generated UUID."""
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return _new_request_id()


class RequestContextMiddleware:
    """Pure ASGI middleware that adds correlation IDs without buffering SSE."""

    def __init__(
        self,
        app: Any,
        *,
        slow_request_ms: float = 2000.0,
        error_rate_min_requests: int = 20,
        error_rate_threshold: float = 0.2,
        error_rate_window_seconds: float = 300.0,
        alert_cooldown_seconds: float = 300.0,
    ) -> None:
        self.app = app
        self.slow_request_ms = slow_request_ms
        self.error_rate_min_requests = error_rate_min_requests
        self.error_rate_threshold = error_rate_threshold
        self.error_rate_window_seconds = error_rate_window_seconds
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self._recent_statuses: deque[tuple[float, int]] = deque()
        self._last_error_rate_alert = float("-inf")

    def _observe_status(self, status_code: int, now: float) -> None:
        self._recent_statuses.append((now, status_code))
        cutoff = now - self.error_rate_window_seconds
        while self._recent_statuses and self._recent_statuses[0][0] < cutoff:
            self._recent_statuses.popleft()
        request_count = len(self._recent_statuses)
        if request_count < self.error_rate_min_requests:
            return
        error_count = sum(status >= 500 for _, status in self._recent_statuses)
        error_rate = error_count / request_count
        if (
            error_rate >= self.error_rate_threshold
            and now - self._last_error_rate_alert >= self.alert_cooldown_seconds
        ):
            self._last_error_rate_alert = now
            log_alert(
                logger,
                "http_error_rate",
                request_count=request_count,
                error_count=error_count,
                error_rate=round(error_rate, 4),
                window_seconds=self.error_rate_window_seconds,
                threshold=self.error_rate_threshold,
            )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_request_id = request_headers.get(REQUEST_ID_HEADER.lower().encode())
        supplied_request_id = (
            raw_request_id.decode("ascii", errors="ignore") if raw_request_id is not None else None
        )
        request_id = normalize_request_id(supplied_request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        token = set_request_id(request_id)
        started_at = perf_counter()
        status_code = 500

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != REQUEST_ID_HEADER.lower().encode()
                ]
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            if status_code >= 500:
                log_level = logging.ERROR
            elif status_code >= 400:
                log_level = logging.WARNING
            else:
                log_level = logging.INFO

            slow = duration_ms >= self.slow_request_ms
            log_event(
                logger,
                log_level,
                "request_completed",
                http_method=scope.get("method", "unknown"),
                http_path=scope.get("path", "unknown"),
                http_status_code=status_code,
                duration_ms=round(duration_ms, 3),
                slow=slow,
            )
            if slow:
                log_alert(
                    logger,
                    "slow_http_request",
                    http_method=scope.get("method", "unknown"),
                    http_path=scope.get("path", "unknown"),
                    duration_ms=round(duration_ms, 3),
                    threshold_ms=self.slow_request_ms,
                )
            self._observe_status(status_code, perf_counter())
            reset_request_id(token)
