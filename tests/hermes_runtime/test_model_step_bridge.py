import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_runtime.model_step_bridge import MODEL_STEP_TOOLS, ModelStepBridge

ROOT = Path(__file__).resolve().parents[2]


def schema_rows():
    spec = importlib.util.spec_from_file_location(
        "s3_step_cli", ROOT / "scripts/run_model_step_tool.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        {"name": n, "description": d, "parameters": s.model_json_schema()}
        for n, (d, s) in module.schemas().items()
    ]


def test_reviewed_contract_matches_bridge_and_effect_classes(monkeypatch):
    bridge = ModelStepBridge("unused", "unused", env={})
    rows = schema_rows()
    monkeypatch.setattr(bridge, "_invoke", lambda **kwargs: rows)
    assert bridge.schemas() == rows
    catalog = json.loads(
        (ROOT / "services/hermes/model-operations.v1.json").read_text(encoding="utf-8")
    )
    assert {row["name"] for row in catalog["operations"]} == MODEL_STEP_TOOLS
    assert [row["effect_kind"] for row in catalog["operations"]] == [
        "derived_write",
        "compute",
        "compute",
        "derived_write",
        "query",
    ]
    rows[0]["parameters"]["properties"]["confirmed"] = {"type": "boolean"}
    with pytest.raises(RuntimeError, match="reviewed v1"):
        bridge.schemas()


def test_native_session_cannot_forge_scope_or_replace_owner(monkeypatch):
    bridge = ModelStepBridge("unused", "unused", env={})
    calls = []

    def invoke(payload):
        calls.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(bridge, "_invoke", invoke)
    handler = bridge.handler("scat_model_prepare_input")
    assert json.loads(handler({}, session_id="forged"))["status"] == "forbidden"
    assert not calls
    bridge.bind("native", "owner", "run")
    handler({"scope": "attacker"}, session_id="native")
    assert calls[0]["scope"] == "owner" and calls[0]["source_run_id"] == "run"
    with pytest.raises(ValueError, match="scope changed"):
        bridge.bind("native", "attacker", "run")


def test_ordinary_backend_rejects_without_any_computation(tmp_path):
    env = os.environ.copy()
    env.pop("SCAT_S3_CPU_FIXTURE", None)
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/run_model_step_tool.py")],
        input=json.dumps(
            {
                "operation": "scat_model_prepare_input",
                "arguments": {},
                "scope": "scope",
                "source_run_id": "run",
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout)["error"] == "approved_job_required_s4"
    assert not list(tmp_path.iterdir())
