"""Bounded execution counters without task identifiers or flight data."""

import logging
import threading
import time
from collections import Counter, deque

from scat_model_runtime.backends.base import BackendError


class ObservedBackend:
    def __init__(self, backend):
        self.backend = backend
        self._lock = threading.Lock()
        self._statuses = Counter()
        self._health_statuses = Counter()
        self._latencies = deque(maxlen=512)
        self._active = 0

    @property
    def config(self):
        return getattr(self.backend, "config", None)

    def health(self):
        health = self.backend.health()
        with self._lock:
            self._health_statuses[health.code] += 1
        return health

    def execute(self, request):
        started = time.perf_counter()
        status = "ok"
        with self._lock:
            self._active += 1
        try:
            return self.backend.execute(request)
        except Exception as exc:
            status = exc.code if isinstance(exc, BackendError) else "execution_failed"
            raise
        finally:
            with self._lock:
                self._active -= 1
                self._statuses[status] += 1
                if status == "ok":
                    self._latencies.append(time.perf_counter() - started)
                emit = status != "ok" or sum(self._statuses.values()) % 32 == 0
            if emit:
                try:
                    logging.getLogger(__name__).info(
                        "model_backend_metrics", extra={"model_backend_metrics": self.snapshot()}
                    )
                except Exception:
                    pass

    def snapshot(self):
        with self._lock:
            values = sorted(self._latencies)

            def percentile(fraction):
                if not values:
                    return None
                index = (len(values) - 1) * fraction
                low = int(index)
                return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (
                    index - low
                )

            circuit = getattr(self.backend, "_circuit", None)
            return {
                "requests": sum(self._statuses.values()),
                "statuses": dict(self._statuses),
                "health_statuses": dict(self._health_statuses),
                "active": self._active,
                "success_latency_samples": len(values),
                "success_p50_seconds": percentile(0.5),
                "success_p95_seconds": percentile(0.95),
                "latency_window_limit": 512,
                "circuit_state": circuit.state if circuit else "not_exposed",
            }

    def cancel(self, request_id):
        return self.backend.cancel(request_id)

    def close(self):
        return self.backend.close()
