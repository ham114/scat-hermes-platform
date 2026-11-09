from __future__ import annotations

import pytest

from core.resilience import CircuitBreaker, CircuitOpenError


def test_circuit_opens_then_allows_one_recovery_probe() -> None:
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=2,
        recovery_timeout_seconds=10.0,
        clock=lambda: now[0],
    )

    breaker.before_call()
    breaker.record_failure()
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    now[0] = 10.0
    assert breaker.state == "half_open"
    breaker.before_call()
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"


def test_failed_half_open_probe_reopens_window() -> None:
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout_seconds=5.0,
        clock=lambda: now[0],
    )
    breaker.record_failure()
    now[0] = 5.0
    breaker.before_call()
    breaker.record_failure()
    now[0] = 9.0
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
