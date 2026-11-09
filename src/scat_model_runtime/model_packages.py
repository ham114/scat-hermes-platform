"""Black-box model package interfaces grouped by task-level I/O contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from scat_model_runtime.component_registry import ComponentRegistry
from scat_model_runtime.contracts import ModelParameterValue, ModelTaskRequest
from scat_model_runtime.task_io import (
    ComparisonTaskInput,
    ComparisonTaskOutput,
    RegressionTaskInput,
    RegressionTaskOutput,
)


class ComparisonModelPackage(Protocol):
    """Run a bounded, common-condition comparison behind one task interface."""

    def start(self) -> None: ...

    def run(
        self,
        *,
        model_input: ComparisonTaskInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> ComparisonTaskOutput: ...

    def stop(self) -> None: ...


class ComparisonTaskInputResolver(Protocol):
    """Resolve authorized SCAT references into the shared comparison input."""

    async def resolve(self, *, request: ModelTaskRequest) -> ComparisonTaskInput: ...


class RegressionModelPackage(Protocol):
    """Own preprocessing, inference and postprocessing behind one task interface."""

    def start(self) -> None: ...

    def run(
        self,
        *,
        model_input: RegressionTaskInput,
        parameters: Mapping[str, ModelParameterValue],
    ) -> RegressionTaskOutput: ...

    def stop(self) -> None: ...


class RegressionTaskInputResolver(Protocol):
    """Resolve authorized business data into the shared regression input type."""

    async def resolve(self, *, request: ModelTaskRequest) -> RegressionTaskInput: ...


regression_model_package_registry = ComponentRegistry[RegressionModelPackage](
    "regression model package"
)
regression_input_resolver_registry = ComponentRegistry[RegressionTaskInputResolver](
    "regression input resolver"
)
comparison_model_package_registry = ComponentRegistry[ComparisonModelPackage](
    "comparison model package"
)
comparison_input_resolver_registry = ComponentRegistry[ComparisonTaskInputResolver](
    "comparison input resolver"
)


__all__ = [
    "ComparisonModelPackage",
    "ComparisonTaskInputResolver",
    "RegressionModelPackage",
    "RegressionTaskInputResolver",
    "comparison_input_resolver_registry",
    "comparison_model_package_registry",
    "regression_input_resolver_registry",
    "regression_model_package_registry",
]
