import copy
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hermes_runtime.authoring_store import AuthoringError, AuthoringStore
from hermes_runtime.experience_learning import verify_automatic_content
from hermes_runtime.script_language import (
    Interpreter,
    ScriptError,
    parse,
    schema_definition,
    validate_value,
)
from hermes_runtime.script_policy import ScriptPolicy
from hermes_runtime.script_resources import descriptors
from hermes_runtime.script_runtime import ScriptRunner, ScriptStore
from tests.hermes_runtime.test_authoring_s7 import content

ROOT = Path(__file__).resolve().parents[2]
OP = "mcp__scat__search_scat_flights"
SOURCE = """def run(inputs):
    response = call("mcp__scat__search_scat_flights", {"batch": inputs["batch"]})
    if get(response, "status") != "ok":
        return {"status": "error", "count": 0}
    items = get(get(response, "data", {}), "items", [])
    return {"status": "ok", "count": len(items)}
"""


def descriptor():
    return {
        "version": 1,
        "runtime": "scat-thin-python-json-v1",
        "operations": [OP],
        "input_schema": {
            "type": "object",
            "properties": {"batch": {"type": "string", "maxLength": 100, "minLength": 1}},
            "required": ["batch"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok", "error"], "maxLength": 10},
                "count": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["status", "count"],
            "additionalProperties": False,
        },
        "cases": [
            {
                "inputs": {"batch": "SYNTH"},
                "calls": [
                    {
                        "operation": OP,
                        "arguments": {"batch": "SYNTH"},
                        "result": {"status": "ok", "data": {"items": [1]}},
                    }
                ],
                "expected": {"status": "ok", "count": 1},
            }
        ],
    }


def script_content():
    result = content("script-query")
    result["changes"][0]["files"].update(
        {"scripts/query.py": SOURCE, "scripts/query.json": json.dumps(descriptor())}
    )
    return result


@pytest.fixture
def workspace(tmp_path):
    for name in ["scripts/run_skill_script_child.py", "src/hermes_runtime/script_language.py"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


@pytest.fixture
def author(tmp_path, monkeypatch):
    monkeypatch.delenv("SCAT_SKILL_RELEASE_STATE", raising=False)
    for folder in ("skills", "services/hermes", "config"):
        shutil.copytree(ROOT / folder, tmp_path / folder)
    monkeypatch.setattr(AuthoringStore, "source_sha", lambda self: "verified-source")
    return AuthoringStore(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef run(inputs):\n return {}",
        "def run(inputs):\n import os\n return {}",
        "def run(inputs):\n return inputs.__class__",
        "def run(inputs):\n return open('/etc/passwd').read()",
        "def run(inputs):\n return __import__('os')",
        "def run(inputs):\n return eval('1')",
        "def run(inputs):\n while True:\n  pass",
        "def run(inputs):\n return [x for x in inputs]",
        "def run(inputs):\n return call(inputs['operation'], {})",
        "def run(inputs):\n return call('terminal', {})",
        "def run(inputs):\n return (lambda: 1)()",
        "def run(inputs):\n inputs['x']=1\n return inputs",
        "def run(inputs):\n return 'x' * 100000000",
        "def run(inputs):\n return run(inputs)",
        "def run(inputs):\n call = 1\n return {}",
        "def run(inputs):\n return get(**inputs)",
    ],
)
def test_unsupported_syntax_cannot_execute(source):
    with pytest.raises(ScriptError):
        parse(source, {OP})


def test_interpreter_branches_loops_and_quota():
    src = "def run(inputs):\n out=[]\n for x in inputs['items']:\n  if x != 0:\n   out=append(out,x)\n return out"
    assert Interpreter(src, [], {"items": [0, 2, 3]}, None).run() == [2, 3]
    with pytest.raises(ScriptError, match="iterations"):
        Interpreter(src, [], {"items": [1, 2]}, None, max_iterations=1).run()
    with pytest.raises(ScriptError, match="steps"):
        Interpreter(src, [], {"items": [1, 2]}, None, max_steps=1).run()
    with pytest.raises(ScriptError, match="too_large"):
        Interpreter(
            "def run(inputs):\n return inputs", [], {"long": "a" * 100}, None, max_value_bytes=20
        ).run()


def test_schema_cannot_install_or_resolve_and_values_strict():
    schema_definition(descriptor()["input_schema"])
    with pytest.raises(ScriptError):
        schema_definition({"$ref": "https://attacker/schema"})
    for invalid in [{"batch": "x", "user": "other"}, {}, {"batch": 1}]:
        with pytest.raises(ScriptError):
            validate_value(invalid, descriptor()["input_schema"])
    with pytest.raises(ScriptError):
        validate_value({"status": "ok", "count": True}, descriptor()["output_schema"])
    with pytest.raises(ValidationError):
        ScriptPolicy(memory_mb=100000)
    for invalid in [
        {"type": []},
        {"type": "integer", "minimum": 0, "maximum": 10**1000},
    ]:
        with pytest.raises(ScriptError):
            schema_definition(invalid)
    with pytest.raises(ScriptError):
        validate_value(10**1000, {"type": "integer", "minimum": 0, "maximum": 100})


def test_author_paired_script_validation_and_signed_bundle(author):
    row = author.write("owner", script_content())
    candidate = author.candidate(row)
    assert len(descriptors(candidate.entries, candidate.files)) == 1
    changed = copy.deepcopy(script_content())
    del changed["changes"][0]["files"]["scripts/query.json"]
    with pytest.raises(ValueError, match="paired"):
        author.candidate(author.write("owner", changed))
    changed = script_content()
    changed["changes"][0]["files"]["scripts/query.py"] = "def run(inputs):\n return open('x')"
    with pytest.raises(ValueError):
        author.candidate(author.write("owner", changed))


def test_script_trial_cannot_pass_with_only_original_s7a_report(author):
    row = author.write("owner", script_content())
    trial = author.begin_trial("owner", row["draft"])
    author.finish_trial(
        trial["id"],
        {
            "status": "passed",
            "bundle_sha256": trial["candidate"],
            "synthetic_only": True,
            "temporary_home_removed": True,
            "owned_processes_remaining": 0,
        },
    )
    assert author.trial("owner", trial["id"])["state"] == "failed"
    with pytest.raises(AuthoringError, match="fresh_successful_trial"):
        author.preview("owner", draft=row["draft"], trial=trial["id"])


def test_automatic_learning_still_cannot_create_scripts():
    payload = script_content()
    payload["changes"][0]["name"] = "auto-example"
    with pytest.raises(ValueError, match="separate_opt_in"):
        verify_automatic_content(
            {"evidence": [{"summary": json.dumps({"operations": [OP]})}]}, payload
        )


def arguments():
    return {
        "scope": "owner-scope",
        "run": "run-one",
        "request_id": str(uuid4()),
        "source": SOURCE,
        "descriptor": descriptor(),
        "inputs": {"batch": "SYNTH"},
        "metadata": {"bundle_sha256": "a" * 64},
    }


def test_real_conda_child_idempotent_concurrent_and_cross_scope(workspace):
    store = ScriptStore(workspace / "receipts.sqlite", workspace)
    runner = ScriptRunner(workspace, store, ScriptPolicy())
    calls = []

    def dispatch(op, args, identity, seq):
        calls.append((op, args))
        time.sleep(0.1)
        return {"status": "ok", "data": {"items": [1]}}, None

    args = arguments()

    def invoke(_):
        try:
            return runner.run(**args, dispatch=dispatch)
        except ScriptError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(invoke, range(4)))
    assert any(v and v["output"]["count"] == 1 for v in outputs)
    assert len(calls) == 1
    assert runner.run(**args, dispatch=dispatch)["replayed"]
    with pytest.raises(ScriptError, match="conflict"):
        runner.run(**(args | {"inputs": {"batch": "other"}}), dispatch=dispatch)
    assert (
        runner.run(**(args | {"scope": "other-owner"}), dispatch=dispatch)["output"]["count"] == 1
    )
    assert len(calls) == 2
    assert list((workspace / ".tmp/skill-scripts").iterdir()) == []


def test_unknown_survives_restart_and_blocks_new_uuid(workspace):
    path = workspace / "receipts.sqlite"
    store = ScriptStore(path, workspace)
    runner = ScriptRunner(workspace, store, ScriptPolicy())
    args = arguments()
    calls = []

    def failed(*_):
        calls.append(1)
        raise OSError("response lost")

    with pytest.raises(ScriptError, match="unknown"):
        runner.run(**args, dispatch=failed)
    restarted = ScriptRunner(workspace, ScriptStore(path, workspace), ScriptPolicy())
    for request in (args["request_id"], str(uuid4())):
        with pytest.raises(ScriptError):
            restarted.run(**(args | {"request_id": request}), dispatch=failed)
    assert calls == [1]
    with closing(store.connect()) as db:
        assert db.execute("SELECT state FROM skill_script_runs").fetchone()[0] == "unknown"


def test_wall_timeout_tracks_inflight_tool_without_replay(workspace):
    store = ScriptStore(workspace / "receipts.sqlite", workspace)
    runner = ScriptRunner(workspace, store, ScriptPolicy(wall_seconds=1))
    calls = []

    def slow(*_):
        calls.append(1)
        time.sleep(1.2)
        return {"status": "ok", "data": {"items": [1]}}, None

    args = arguments()
    with pytest.raises(ScriptError, match="unknown"):
        runner.run(**args, dispatch=slow)
    with pytest.raises(ScriptError):
        runner.run(**args, dispatch=slow)
    assert calls == [1]


def test_worker_restart_does_not_fence_another_live_job(workspace):
    path = workspace / "shared.sqlite"
    store = ScriptStore(path, workspace)
    first, _ = store.claim("owner", "job_one", uuid4(), "one", {}, ScriptPolicy())
    second, _ = store.claim("owner", "job_two", uuid4(), "two", {}, ScriptPolicy())
    ScriptStore(path, workspace, recover_namespace=("owner", "job_one"))
    with closing(store.connect()) as db:
        states = dict(db.execute("SELECT id,state FROM skill_script_runs"))
    assert states[first["id"]] == "unknown"
    assert states[second["id"]] == "running"


def test_native_bridge_retains_load_budget_and_scoped_receipts(author, workspace):
    from application.context import ExecutionContext
    from hermes_runtime.results import ToolResultStore
    from hermes_runtime.script_bridge import SCRIPT_TOOL, ScriptBridge
    from hermes_runtime.skill_budget import RunBudget
    from hermes_runtime.skill_guard import SkillOperationGuard
    from hermes_runtime.skill_policy import Budget
    from hermes_runtime.skills import NativeSkillBridge
    from tests.hermes_runtime.test_skills import Native

    row = author.write("owner", script_content())
    release = author.candidate(row)
    folder = release.stage(workspace / "native")
    bridge = NativeSkillBridge(release, folder, Native(folder), workspace / "trace.sqlite")
    context = ExecutionContext("owner", "thread")
    bridge.bind("session", context.scope_key, "run_script")
    budget = RunBudget(bridge.trace_path, context.scope_key, "run_script", Budget(), "policy")
    bridge._budgets["session"] = budget
    count = []

    class Registry:
        def dispatch(self, name, args, **kwargs):
            if name == SCRIPT_TOOL:
                return scripts.handler(args, **kwargs)
            count.append(1)
            return {"status": "ok", "data": {"items": [1]}}

    registry = Registry()
    results = ToolResultStore(workspace / "results.sqlite", workspace_root=workspace)
    scripts = ScriptBridge(workspace, bridge, registry, results, workspace / "script.sqlite")
    SkillOperationGuard(bridge, {OP}, {SCRIPT_TOOL}).install(registry)
    args = {
        "skill": "script-query",
        "script": "scripts/query.py",
        "inputs": {"batch": "SYNTH"},
        "request_id": str(uuid4()),
    }
    assert not json.loads(registry.dispatch(SCRIPT_TOOL, args, session_id="forged"))["success"]
    assert not json.loads(registry.dispatch(SCRIPT_TOOL, args, session_id="session"))["success"]
    assert count == []
    assert json.loads(bridge.handler("skill_view")({"name": "script-query"}, session_id="session"))[
        "success"
    ]
    first = json.loads(registry.dispatch(SCRIPT_TOOL, args, session_id="session"))
    assert first["success"] and first["output"]["count"] == 1
    assert json.loads(registry.dispatch(SCRIPT_TOOL, args, session_id="session"))["replayed"]
    assert count == [1] and budget.snapshot()["tool_calls"] == 4
    assert len(results.list(context.scope_key, "run_script")) == 1
    scripts.approved_skills = {}
    scripts.approved_operations = frozenset({OP})
    assert json.loads(scripts.handler(args, session_id="session"))["error"] == (
        "script_outside_approved_job"
    )
    scripts.approved_skills = {"script-query": "1.0.0"}
    scripts.approved_operations = frozenset()
    assert json.loads(scripts.handler(args, session_id="session"))["error"] == (
        "script_outside_approved_job"
    )
    scripts.approved_operations = frozenset({OP})
    budget.finish()
    assert not json.loads(
        scripts.handler(args | {"request_id": str(uuid4())}, session_id="session")
    )["success"]
