"""Task package facade; evaluation and true-future alignment stay in the backend host."""

import asyncio
import threading
from uuid import uuid4

from scat_model_runtime.backends.base import BackendError, InferenceRequest, run_identity
from scat_model_runtime.task_io import RegressionTaskOutput


class BackendRegressionPackage:
    def __init__(self, backend, *, model_key="scat-gbar-v23", model_version="23"):
        self.backend = backend
        self.model_key, self.model_version = model_key, model_version
        self._lock = threading.Lock()
        self._runs: dict[str, set[str]] = {}

    def start(self):
        health = self.backend.health()
        if not health.ready:
            raise BackendError(health.code)

    def run(self, *, model_input, parameters):
        request = InferenceRequest(
            request_id=uuid4().hex,
            run_id=run_identity.get() or uuid4().hex,
            model_key=self.model_key,
            model_version=self.model_version,
            input_schema=model_input.schema_version,
            output_schema="regression-task-output/v1",
            payload=model_input.model_dump(mode="json"),
            parameters=dict(parameters),
        )
        with self._lock:
            self._runs.setdefault(request.run_id, set()).add(request.request_id)
        try:
            result = self.backend.execute(request)
            result.verify_identity(request)
            return RegressionTaskOutput.model_validate(result.payload)
        except BaseException:
            self.backend.cancel(request.request_id)
            raise
        finally:
            with self._lock:
                current = self._runs.get(request.run_id, set())
                current.discard(request.request_id)
                if not current:
                    self._runs.pop(request.run_id, None)

    def cancel_run(self, run_id):
        with self._lock:
            requests = tuple(self._runs.get(run_id, ()))
        for request_id in requests:
            self.backend.cancel(request_id)

    def stop(self):
        self.backend.close()


async def execute_with_identity(package, *, model_input, parameters, run_id, timeout):
    token = run_identity.set(run_id)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(package.run, model_input=model_input, parameters=parameters), timeout
        )
    except (asyncio.CancelledError, TimeoutError):
        cancel = getattr(package, "cancel_run", None)
        if cancel is not None:
            await asyncio.to_thread(cancel, run_id)
        raise
    finally:
        run_identity.reset(token)
