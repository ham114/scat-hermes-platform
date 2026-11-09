"""Actual native bridge restart over an old home; no model, database query or inference."""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

root = Path.cwd()
home = root / ".tmp" / ("forecast-restart-" + uuid4().hex)
home.mkdir()
try:
    os.environ.update(
        HERMES_HOME=str(home),
        HERMES_DISABLE_LAZY_INSTALLS="1",
        TMPDIR=str(home),
        TMP=str(home),
        TEMP=str(home),
    )
    (home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": []}}))
    sys.path.insert(0, str(root / "vendor/hermes-agent"))
    from hermes_runtime.release_store import ReleaseStore
    from hermes_runtime.skills import register_native_skills

    store = ReleaseStore(root)
    target = store.load(os.environ["FORECAST_TARGET_BUNDLE"])
    old = store.load(os.environ["FORECAST_PREVIOUS_BUNDLE"])
    old.stage(home)
    before = {
        str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (home / "skills").rglob("*")
        if p.is_file()
    }
    trusted = SimpleNamespace(pin=lambda scope, run, sha=None: target)
    bridge = register_native_skills(
        target, home, root / "vendor/hermes-agent", home / "trace.sqlite", store=trusted
    )
    bridge.bind("probe-session", "synthetic-scope", "synthetic-run")
    catalog = json.loads(bridge.handler("skills_list")({}, session_id="probe-session"))
    entry = next(v for v in catalog["skills"] if v["name"] == "scat-model-comparison")
    assert entry["version"] == "0.3.0"
    body = json.loads(
        bridge.handler("skill_view")({"name": "scat-model-comparison"}, session_id="probe-session")
    )
    assert body["success"] and "kind=forecast" in body["content"]
    after = {
        str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (home / "skills").rglob("*")
        if p.is_file()
    }
    assert before == after
    from hermes_runtime.skill_guard import SkillOperationGuard

    guard = SkillOperationGuard(bridge, {"scat_workflow_prepare"}, set())
    bridge.bind("guard-session", "synthetic-scope", "new-run")
    calls = []

    def dispatch(*args, **kwargs):
        calls.append(True)
        return json.dumps({"status": "ok", "synthetic": True})

    args = {"proposal": {"kind": "forecast"}}
    denied = json.loads(
        guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="guard-session")
    )
    assert denied["recovery"]["skills"] == [
        {"name": "scat-model-comparison", "version": "0.3.0", "file_path": "SKILL.md"}
    ]
    body = json.loads(
        bridge.handler("skill_view")({"name": "scat-model-comparison"}, session_id="guard-session")
    )
    assert body["success"]
    accepted = json.loads(
        guard.invoke(dispatch, "scat_workflow_prepare", args, session_id="guard-session")
    )
    assert accepted["status"] == "ok" and len(calls) == 1
    print(
        json.dumps(
            {
                "status": "passed",
                "old_home_retained": True,
                "new_version_loaded": "0.3.0",
                "versioned_folder": bridge.folder
                == home / "releases" / target.bundle_sha256 / "skills",
                "forecast_guard_recovery_and_dispatch": True,
                "llm_calls": 0,
                "business_queries": 0,
                "prediction_calls": 0,
            }
        )
    )
finally:
    assert home.resolve().parent == root.resolve() / ".tmp"
    shutil.rmtree(home)
