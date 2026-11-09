"""Explicit server-owned versioned transport registrations."""

from scat_model_runtime.backends.base import ExecutionBackend
from scat_model_runtime.component_registry import ComponentRegistry

execution_backend_registry = ComponentRegistry[ExecutionBackend]("execution backend")
