from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_runtime.rag_operation import TOOL, RagOperation, register_knowledge_rag
from hermes_runtime.skill_budget import RunBudget
from hermes_runtime.skill_policy import Budget

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Linux process locks; verified in server Conda"
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup(tmp_path):
    root = tmp_path
    (root / "var").mkdir()
    text = "样本规则要求20个点。"
    row = {
        "chunk_id": "c1",
        "source_id": "rules",
        "source_version": "v1",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    corpus = root / "corpus.jsonl"
    corpus.write_text(json.dumps(row, ensure_ascii=False))
    config = json.loads((ROOT / "services/rag/real-agent-v1.json").read_text()) | {
        "corpus": str(corpus),
        "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
        "corpus_count": 1,
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config))
    budget = RunBudget(root / "var/parent.sqlite", "scope-a", "run", Budget(), "policy")
    other = RunBudget(root / "var/other.sqlite", "scope-b", "run", Budget(), "policy")
    bridge = SimpleNamespace(
        _sessions={"known": ("scope-a", "run"), "other": ("scope-b", "run")},
        _budgets={"known": budget, "other": other},
    )
    bridge.session_bridge = lambda sid: bridge
    calls = []

    def execute(command, identity, shared):
        calls.append(command)
        descriptor = Path(command[command.index("--shared-budget") + 1])
        assert descriptor.stat().st_mode & 0o777 == 0o600
        parent = json.loads(descriptor.read_text())
        assert parent["path"] == str(budget.path) and parent["scope"] == "scope-a"
        shared.wrap_create(
            lambda **kwargs: SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 1})
        )(messages=[{"role": "user", "content": "fixture"}], max_tokens=10)
        reference = {
            k: row[k] for k in ("chunk_id", "source_id", "source_version", "text_sha256")
        } | {"scope": "scope-a", "run_id": "nested", "corpus_sha256": config["corpus_sha256"]}
        result = {
            "answer": text,
            "status": "answerable",
            "claims": [
                {
                    "claim": text,
                    "evidence": [
                        {
                            "reference": reference,
                            "quote": text,
                            "start_char": 0,
                            "end_char": len(text),
                        }
                    ],
                }
            ],
            "targets": [],
            "coverage": [],
            "receipt_id": "receipt",
            "answer_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        return {"status": "passed", "mode": "deep", "run_id": "nested", "result": result}

    operation = RagOperation(root, root / "var/rag", bridge, config_path, executor=execute)
    return operation, bridge, budget, calls, execute


def test_trusted_budget_is_shared_and_duplicate_survives_reopen(setup):
    operation, bridge, budget, calls, execute = setup
    first = operation.invoke("样本规则？", "deep", "known")
    assert first["status"] == "ok" and not first["reused"]
    assert budget.snapshot()["llm_calls"] == 1
    reopened = RagOperation(
        operation.root, operation.state_root, bridge, operation.config_path, executor=execute
    )
    second = reopened.invoke("样本规则？", "deep", "known")
    assert second["request_id"] == first["request_id"] and second["reused"]
    assert len(calls) == 1
    assert reopened.status(first["request_id"], "other") == {"status": "not_found"}
    assert reopened.cancel(first["request_id"], "known")["status"] == "ok"


def test_deep_question_preserves_trusted_turn_and_clears_after_completion(setup):
    operation, _, _, _, _ = setup
    original_question = "文件名和单位分别是什么？"
    observed = []
    agent = SimpleNamespace(session_id="known")

    def run(**kwargs):
        observed.append(operation.question_for("known", "关键词"))
        assert operation.question_for("other", "其他会话的问题") == "其他会话的问题"
        return "completed"

    agent.run_conversation = run
    operation.bind_agent_question(agent)
    assert agent.run_conversation(user_message=original_question) == "completed"
    assert observed == [original_question]
    assert operation.question_for("known", "新问题") == "新问题"


def test_question_binding_rejects_changed_owner_and_clears_on_failure(setup):
    operation, bridge, _, _, _ = setup
    agent = SimpleNamespace(session_id="known")

    def run(**kwargs):
        bridge._sessions["known"] = bridge._sessions["other"]
        bridge._budgets["known"] = bridge._budgets["other"]
        with pytest.raises(ValueError, match="owner_changed"):
            operation.question_for("known", "关键词")
        raise RuntimeError("provider_failure")

    agent.run_conversation = run
    operation.bind_agent_question(agent)
    with pytest.raises(RuntimeError, match="provider_failure"):
        agent.run_conversation(user_message="原问题")
    assert not operation._questions


@pytest.mark.parametrize("field", ["scope", "digest", "quote", "mode"])
def test_tampered_child_output_becomes_unknown_and_is_not_replayed(setup, field):
    operation, _bridge, _budget, calls, original = setup

    def corrupt(*args):
        report = original(*args)
        if field == "scope":
            report["result"]["claims"][0]["evidence"][0]["reference"]["scope"] = "other"
        elif field == "digest":
            report["result"]["answer"] = "changed"
        elif field == "quote":
            report["result"]["claims"][0]["evidence"][0]["quote"] = "not present"
        else:
            report["mode"] = "fast"
        return report

    operation.executor = corrupt
    first = operation.invoke("question", "deep", "known")
    assert first["status"] == "unknown"
    assert operation.invoke("question", "deep", "known")["status"] == "unknown"
    assert len(calls) == 1


def test_interrupted_start_is_unknown_after_restart_without_replay(setup):
    operation, bridge, _budget, calls, original = setup

    def fail(*args):
        raise TimeoutError("fixture process interrupted")

    operation.executor = fail
    first = operation.invoke("question", "deep", "known")
    reopened = RagOperation(
        operation.root, operation.state_root, bridge, operation.config_path, executor=original
    )
    assert reopened.invoke("question", "deep", "known")["request_id"] == first["request_id"]
    assert not calls


def test_poll_during_live_controller_launch_does_not_corrupt_state(setup):
    import fcntl

    operation, _bridge, _budget, _calls, _execute = setup
    first = operation.invoke("question", "deep", "known")
    operation._update(first["request_id"], status="starting", result=None)
    with (operation.state_root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert operation.status(first["request_id"], "known")["status"] == "starting"
    assert operation.status(first["request_id"], "known")["status"] == "unknown"


def test_cancel_before_launch_cannot_be_replaced_by_running(setup):
    operation, _bridge, budget, _calls, _execute = setup
    first = operation.invoke("question", "deep", "known")
    operation._update(first["request_id"], status="starting", result=None)
    operation.cancel(first["request_id"], "known")
    with pytest.raises(RuntimeError, match="cancelled_before_launch"):
        operation._execute([], first["request_id"], budget)
    assert operation._row(first["request_id"])["status"] == "unknown"


def test_finished_or_mismatched_parent_budget_cannot_launch(setup):
    operation, bridge, budget, calls, _execute = setup
    bridge._sessions["known"] = ("forged", "run")
    with pytest.raises(ValueError, match="trusted_parent"):
        operation.invoke("question", "deep", "known")
    bridge._sessions["known"] = ("scope-a", "run")
    budget.finish()
    assert operation.invoke("question", "deep", "known")["status"] == "budget_exhausted"
    assert not calls


def test_corrupted_cached_answer_is_not_returned(setup):
    operation, _bridge, _budget, _calls, _execute = setup
    first = operation.invoke("question", "deep", "known")
    row = operation._row(first["request_id"])
    value = json.loads(row["result"])
    value["answer"] = "corrupt"
    with sqlite3.connect(operation.path) as db:
        db.execute(
            "UPDATE rag_invocations SET result=? WHERE id=?",
            (json.dumps(value), first["request_id"]),
        )
    assert operation.status(first["request_id"], "known")["status"] == "unknown"


def test_cross_scope_cancel_and_pid_reuse_do_not_signal_a_process(setup, monkeypatch):
    operation, _bridge, _budget, _calls, _execute = setup
    first = operation.invoke("question", "deep", "known")
    assert operation.cancel(first["request_id"], "other") == {"status": "not_found"}
    monkeypatch.setattr("hermes_runtime.rag_operation.process_identity", lambda pid: "new-birth")
    assert not operation.owned_process({"pid": 123, "birth": "old-birth"})


def test_registration_requires_guard_and_default_fast_keeps_original_binding(setup):
    operation, bridge, _budget, calls, _execute = setup
    schema = {
        "name": TOOL,
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    original = SimpleNamespace(
        schema=schema,
        is_async=False,
        toolset="scat",
        handler=lambda arguments, **kw: json.dumps({"legacy": arguments}),
        check_fn=None,
        requires_env=[],
    )
    registry = SimpleNamespace(get_entry=lambda name: original)
    with pytest.raises(ValueError, match="installed_skill_guard"):
        register_knowledge_rag(
            registry,
            bridge,
            operation.root,
            operation.root / "var/registered",
            operation.config_path,
        )
    registry._scat_skill_guard = SimpleNamespace(bridge=bridge)
    registered = {}
    registry.register = lambda **kw: registered.update(kw)
    register_knowledge_rag(
        registry, bridge, operation.root, operation.root / "var/registered", operation.config_path
    )
    reply = json.loads(registered["handler"]({"query": "question"}, session_id="known"))
    assert reply == {"legacy": {"query": "question"}}
    assert not calls
    assert registered["schema"]["parameters"]["properties"]["mode"]["enum"] == [
        "fast",
        "deep",
        "auto",
    ]
    assert "mode" not in original.schema["parameters"]["properties"]
