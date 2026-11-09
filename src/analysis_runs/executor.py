"""Process-local execution registry for durable asynchronous model runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

type AnalysisRunFactory = Callable[[], Awaitable[Any]]


class AnalysisRunExecutor:
    """Track background coroutines by durable run ID.

    MongoDB remains the source of truth for status and results.  This registry
    only owns live asyncio tasks so the API can return immediately and cancel
    work running in this service process.
    """

    def __init__(self, *, max_concurrency: int = 1) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_concurrency)

    async def submit(self, run_id: str, factory: AnalysisRunFactory) -> None:
        async with self._lock:
            active = self._tasks.get(run_id)
            if active is not None and not active.done():
                raise ValueError(f"analysis run {run_id} is already executing")
            task = asyncio.create_task(
                self._run_and_release(run_id, factory),
                name=f"analysis-run-{run_id}",
            )
            self._tasks[run_id] = task

    async def _run_and_release(
        self,
        run_id: str,
        factory: AnalysisRunFactory,
    ) -> None:
        try:
            async with self._slots:
                await factory()
        finally:
            current = asyncio.current_task()
            async with self._lock:
                if self._tasks.get(run_id) is current:
                    self._tasks.pop(run_id, None)

    async def cancel(self, run_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(run_id)
            if task is None or task.done():
                return False
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def shutdown(self) -> tuple[str, ...]:
        async with self._lock:
            active = tuple(
                (run_id, task) for run_id, task in self._tasks.items() if not task.done()
            )
            for _, task in active:
                task.cancel()
        if active:
            await asyncio.gather(*(task for _, task in active), return_exceptions=True)
        return tuple(run_id for run_id, _ in active)

    async def active_run_ids(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(run_id for run_id, task in self._tasks.items() if not task.done())


analysis_run_executor = AnalysisRunExecutor()


__all__ = ["AnalysisRunExecutor", "analysis_run_executor"]
