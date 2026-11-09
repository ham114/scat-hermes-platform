from uuid import uuid4

import pytest

from application.context import ExecutionContext
from hermes_runtime.registry import RunRegistry, ThreadBusyError


def test_three_users_have_separate_atomic_admissions_and_replays_do_not_spend_slots(tmp_path):
    store = RunRegistry(
        tmp_path / "state.sqlite",
        workspace_root=tmp_path,
        max_active_per_owner=1,
        max_active_total=3,
    )
    contexts = [
        ExecutionContext(owner, store.create_thread(owner)["thread_id"])
        for owner in ["hyn", "zyj", "admin"]
    ]
    for context in contexts:
        store.reserve_request(context, "request", "a" * 64)
    store.reserve_request(contexts[0], "request", "a" * 64)
    extra = ExecutionContext("hyn", store.create_thread("hyn")["thread_id"])
    with pytest.raises(ThreadBusyError):
        store.reserve_request(extra, "extra", "b" * 64)
    fourth = ExecutionContext("fourth", store.create_thread("fourth")["thread_id"])
    with pytest.raises(ThreadBusyError):
        store.reserve_request(fourth, "fourth", "c" * 64)
    store.bind(contexts[0], "request", "run_one")
    store.finish_run(contexts[0], "run_one")
    store.reserve_request(extra, str(uuid4()), "b" * 64)
