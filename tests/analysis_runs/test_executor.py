"""Tests for the in-process owner of live asynchronous analysis tasks."""

from __future__ import annotations

import asyncio

import pytest

from analysis_runs import AnalysisRunExecutor


@pytest.mark.asyncio
async def test_executor_runs_and_releases_completed_task():
    executor = AnalysisRunExecutor()
    completed = asyncio.Event()

    async def run() -> None:
        completed.set()

    await executor.submit("a" * 32, run)
    await asyncio.wait_for(completed.wait(), timeout=1)
    for _ in range(10):
        if not await executor.active_run_ids():
            break
        await asyncio.sleep(0)

    assert await executor.active_run_ids() == ()


@pytest.mark.asyncio
async def test_executor_cancels_live_task_once():
    executor = AnalysisRunExecutor()
    started = asyncio.Event()
    released = asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    run_id = "b" * 32
    await executor.submit(run_id, run)
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await executor.cancel(run_id) is True
    assert released.is_set()
    assert await executor.cancel(run_id) is False
    assert await executor.active_run_ids() == ()


@pytest.mark.asyncio
async def test_executor_rejects_duplicate_live_run_id():
    executor = AnalysisRunExecutor()
    started = asyncio.Event()

    async def run() -> None:
        started.set()
        await asyncio.Event().wait()

    run_id = "c" * 32
    await executor.submit(run_id, run)
    await asyncio.wait_for(started.wait(), timeout=1)

    with pytest.raises(ValueError, match="already executing"):
        await executor.submit(run_id, run)

    assert await executor.shutdown() == (run_id,)


@pytest.mark.asyncio
async def test_executor_bounds_concurrent_model_runs():
    executor = AnalysisRunExecutor(max_concurrency=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first() -> None:
        first_started.set()
        await release_first.wait()

    async def second() -> None:
        second_started.set()

    await executor.submit("d" * 32, first)
    await executor.submit("e" * 32, second)
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not second_started.is_set()

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await executor.shutdown()
