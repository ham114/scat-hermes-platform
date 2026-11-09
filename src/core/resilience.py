"""Explicit bounded-failure primitives for local and external dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from time import monotonic


class CircuitOpenError(RuntimeError):
    """Raised when a dependency is temporarily blocked after repeated failures."""


class CircuitBreaker:
    """Thread-safe closed/open/half-open breaker with a monotonic recovery clock."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")
        self.failure_threshold = int(failure_threshold)
        self.recovery_timeout_seconds = float(recovery_timeout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._clock() - self._opened_at >= self.recovery_timeout_seconds:
                return "half_open"
            return "open"

    def before_call(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if self._clock() - self._opened_at < self.recovery_timeout_seconds:
                raise CircuitOpenError("dependency circuit is open")
            if self._half_open_in_flight:
                raise CircuitOpenError("dependency circuit half-open probe is in flight")
            self._half_open_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._half_open_in_flight = False
            self._failures += 1
            if self._opened_at is not None or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()


__all__ = ["CircuitBreaker", "CircuitOpenError"]
