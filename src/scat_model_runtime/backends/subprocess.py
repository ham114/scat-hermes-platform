"""Adapt the existing resident package without changing its numerical behavior."""

from __future__ import annotations

import threading

from scat_model_runtime.backends.base import (
    BackendError,
    BackendHealth,
    CancelResult,
    InferenceRequest,
    InferenceResult,
)
from scat_model_runtime.task_io import ModelTaskDataError, RegressionTaskInput


class SubprocessBackend:
    def __init__(self, package, *, model_key: str, model_version: str):
        self.package = package
        self.model_key, self.model_version = model_key, model_version
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._active: str | None = None
        self._closed = False
        self._cancelled: set[str] = set()

    def health(self):
        return BackendHealth(not self._closed, "closed" if self._closed else "ready")

    def execute(self, request: InferenceRequest):
        if not self._lock.acquire(blocking=False):
            raise BackendError("busy")
        try:
            with self._state:
                if self._closed:
                    raise BackendError("closed")
                if (request.model_key, request.model_version) != (
                    self.model_key,
                    self.model_version,
                ):
                    raise BackendError("invalid_input")
                self._active = request.request_id
            try:
                model_input = RegressionTaskInput.model_validate(request.payload)
            except ValueError:
                raise BackendError("invalid_input") from None
            if request.parameters:
                raise BackendError("invalid_input")
            if (
                request.input_schema != model_input.schema_version
                or request.output_schema != "regression-task-output/v1"
            ):
                raise BackendError("invalid_input")
            self.package.start()
            output = self.package.run(model_input=model_input, parameters=request.parameters)
            with self._state:
                if request.request_id in self._cancelled:
                    raise BackendError("cancelled", execution_state="unknown")
            result = InferenceResult(
                **{
                    key: getattr(request, key)
                    for key in (
                        "request_id",
                        "run_id",
                        "model_key",
                        "model_version",
                        "input_schema",
                        "output_schema",
                    )
                },
                payload=output.model_dump(mode="json"),
            )
            result.verify_identity(request)
            if output.schema_version != request.output_schema or (
                output.model_key,
                output.model_version,
            ) != (request.model_key, request.model_version):
                raise BackendError("invalid_response", execution_state="unknown")
            return result
        except BackendError:
            raise
        except ModelTaskDataError:
            # The legacy package may have computed earlier members of a batch.
            raise BackendError("data_unrecoverable", execution_state="unknown") from None
        except Exception:
            with self._state:
                if request.request_id in self._cancelled:
                    raise BackendError("cancelled", execution_state="unknown") from None
            raise BackendError("inference_failed", execution_state="unknown") from None
        finally:
            with self._state:
                self._active = None
                self._cancelled.discard(request.request_id)
            self._lock.release()

    def cancel(self, request_id: str):
        with self._state:
            if self._active != request_id:
                return CancelResult(False, "not_started")
            self._cancelled.add(request_id)
        interrupt = getattr(self.package.adapter, "interrupt", None)
        if interrupt is not None:
            interrupt()
        return CancelResult(True, "unknown")

    def close(self):
        with self._state:
            self._closed = True
            active = self._active
        if active:
            self.cancel(active)
        self.package.stop()
