"""Opt-in, operator-selected GBAR shadow requests with no result-store access."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import threading
import time
from copy import deepcopy
from pathlib import Path

from scat_model_runtime.backends.base import BackendError


def compare_payloads(actual, expected):
    """Only classification probabilities use the original frozen tolerances."""
    maximum = 0.0

    def equal(a, b, probability=False):
        nonlocal maximum
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(
                equal(a[key], b[key], probability or key == "class_probabilities") for key in a
            )
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(equal(x, y, probability) for x, y in zip(a, b))
        if probability and type(a) in (float, int) and type(b) in (float, int):
            maximum = max(maximum, abs(a - b))
            return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-6 + 1e-5 * abs(b)
        return type(a) is type(b) and a == b

    return {"equivalent": equal(actual, expected), "maximum_probability_difference": maximum}


class ShadowBackend:
    """One in-flight shadow, zero waiting queue; the primary remains authoritative."""

    def __init__(self, primary, candidate, *, references, report_directory):
        self.primary, self.candidate = primary, candidate
        self.references = frozenset(references)
        if not 1 <= len(self.references) <= 100 or any(
            not isinstance(ref, str) or "/" not in ref or "*" in ref or len(ref) > 240
            for ref in self.references
        ):
            raise ValueError("shadow requires exact operator-selected references")
        self.directory = Path(report_directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / ("shadow-" + secrets.token_hex(8) + ".jsonl")
        self._salt = secrets.token_bytes(32)
        self._slot = threading.Lock()
        self._state = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        self._counts = {
            key: 0
            for key in [
                "selected",
                "completed",
                "busy_dropped",
                "failed",
                "mismatched",
                "report_errors",
            ]
        }

    def health(self):
        return self.primary.health()

    def _alias(self, value):
        return "shadow-" + hashlib.sha256(self._salt + value.encode()).hexdigest()[:32]

    def _deidentify(self, payload):
        value = deepcopy(payload)
        for series in value["series"]:
            ref = series["input_ref"]
            ref["external_id"] = self._alias(ref["external_id"])
            ref["batch"] = self._alias(ref.get("batch") or "")
        return value

    def _record(self, report):
        try:
            # Bound the private report file; never log inputs, outputs or raw IDs.
            if self.path.exists() and self.path.stat().st_size >= 1_000_000:
                raise OSError("shadow report limit")
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(report, allow_nan=False) + "\n")
        except Exception:
            with self._state:
                self._counts["report_errors"] += 1

    def _run(self, request, expected):
        started = time.perf_counter()
        report = {
            "request_alias": request.request_id,
            "model_key": request.model_key,
            "model_version": request.model_version,
            "status": "failed",
        }
        try:
            result = self.candidate.execute(request)
            result.verify_identity(request)
            report.update(compare_payloads(result.payload, expected))
            report["status"] = "equivalent" if report["equivalent"] else "mismatch"
        except Exception as exc:
            report["error_code"] = exc.code if isinstance(exc, BackendError) else "shadow_failed"
        finally:
            report["seconds"] = time.perf_counter() - started
            self._record(report)
            with self._state:
                self._counts["completed"] += 1
                self._counts["failed"] += report["status"] == "failed"
                self._counts["mismatched"] += report["status"] == "mismatch"
                self._slot.release()
                self._idle.set()

    def execute(self, request):
        result = self.primary.execute(request)
        result.verify_identity(request)
        series = request.payload.get("series", [])
        selected = bool(series) and all(
            (item.get("input_ref", {}).get("batch") or "")
            + "/"
            + item.get("input_ref", {}).get("external_id", "")
            in self.references
            for item in series
        )
        if not selected or self._closed:
            return result
        with self._state:
            self._counts["selected"] += 1
            if not self._slot.acquire(blocking=False):
                self._counts["busy_dropped"] += 1
                return result
            self._idle.clear()
        try:
            candidate_request = request.model_copy(
                update={
                    "request_id": self._alias(request.request_id),
                    "run_id": self._alias(request.run_id),
                    "payload": self._deidentify(request.payload),
                    "parameters": deepcopy(request.parameters),
                }
            )
            expected = self._deidentify(result.payload)
            threading.Thread(
                target=self._run, args=(candidate_request, expected), daemon=True
            ).start()
        except Exception:
            with self._state:
                self._slot.release()
                self._idle.set()
                self._counts["failed"] += 1
        return result

    def snapshot(self):
        with self._state:
            return {**self._counts, "in_flight": not self._idle.is_set(), "queue_limit": 0}

    def wait_idle(self, timeout):
        return self._idle.wait(timeout)

    def cancel(self, request_id):
        # A shadow never changes the authoritative cancellation decision.
        return self.primary.cancel(request_id)

    def close(self):
        self._closed = True
        try:
            self.candidate.close()
            self.wait_idle(5)
        finally:
            self.primary.close()
