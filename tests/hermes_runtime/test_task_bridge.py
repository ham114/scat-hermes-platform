import hashlib
import json
from types import SimpleNamespace

import pytest

from hermes_runtime.registry import RunRegistry, ThreadNotFoundError
from hermes_runtime.task_bridge import TASK_TOOLS, TaskToolBridge


def test_native_binding_never_uses_model_supplied_identity(monkeypatch):
    bridge = TaskToolBridge("conda-python", "private-script", env={})
    calls = []
    monkeypatch.setattr(
        bridge, "_invoke", lambda payload: calls.append(payload) or {"status": "ok"}
    )
    handler = bridge.handler("scat_task_prepare")
    assert json.loads(handler({}, session_id="unknown"))["status"] == "forbidden"
    bridge.bind("native", "scat:trusted", "run_trusted")
    handler(
        {"scope": "scat:forged", "source_run_id": "run_forged"},
        session_id="native",
    )
    assert calls[0]["scope"] == "scat:trusted"
    assert calls[0]["source_run_id"] == "run_trusted"
    expected = hashlib.sha256(
        json.dumps(
            calls[0]["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert calls[0]["proposal_key"] == "run_trusted:" + expected
    # Actual Hermes registry kwargs: task_id/session_id/user_task, no tool_call_id.
    handler(calls[0]["arguments"], session_id="native", task_id="native-task", user_task="text")
    assert calls[1]["proposal_key"] == calls[0]["proposal_key"]
    with pytest.raises(ValueError):
        bridge.bind("native", "scat:other", "run_other")
    with pytest.raises(ValueError):
        bridge.handler("submit_model_task")
    assert not {"submit_model_task", "confirm_model_task", "terminal"} & TASK_TOOLS


def test_backend_error_details_and_output_are_bounded(monkeypatch):
    bridge = TaskToolBridge("python", "script", env={})
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="private-token")
    )
    assert "private-token" not in json.dumps(bridge._invoke({}))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="x" * 524289)
    )
    assert bridge._invoke({})["status"] == "unavailable"


def test_scope_lookup_reuses_platform_owner_registry(tmp_path):
    registry = RunRegistry(tmp_path / "runtime.sqlite", workspace_root=tmp_path)
    thread = registry.create_thread("owner")
    context = registry.require_thread("owner", thread["thread_id"])
    assert registry.require_scope("owner", context.scope_key) == context
    with pytest.raises(ThreadNotFoundError):
        registry.require_scope("other", context.scope_key)


def test_control_scope_uses_admitted_owner_and_refuses_cross_run_pairing(tmp_path):
    registry = RunRegistry(tmp_path / "runtime.sqlite", workspace_root=tmp_path)
    contexts = []
    for index, owner in enumerate(["scat-researcher", "scat-zyj", "scat-admin"]):
        thread = registry.create_thread(owner)
        context = registry.require_thread(owner, thread["thread_id"])
        registry.reserve_request(context, str(index), "a" * 64)
        registry.bind(context, str(index), "run_control_" + str(index))
        contexts.append(context)
        assert (
            registry.require_control_scope(context.scope_key, "run_control_" + str(index))
            == context
        )
    with pytest.raises(ThreadNotFoundError):
        registry.require_control_scope(contexts[1].scope_key, "run_control_0")
    with pytest.raises(ThreadNotFoundError):
        registry.require_control_scope("scat:" + "0" * 64, "run_control_0")
    with registry._connect() as db:
        db.execute(
            "UPDATE runtime_admission_owners SET owner_id=? WHERE scope_key=?",
            ("scat-admin", contexts[1].scope_key),
        )
    with pytest.raises(ThreadNotFoundError):
        registry.require_control_scope(contexts[1].scope_key, "run_control_1")
