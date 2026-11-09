import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.readonly_extensions import ALL_READ_ONLY_TOOLS
from hermes_runtime.skill_guard import SkillOperationGuard
from hermes_runtime.skills import NativeSkillBridge, SkillPolicyError, SkillRelease


@pytest.fixture
def copied_release(tmp_path):
    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "skills", tmp_path / "skills")
    manifest = json.loads((root / "services/hermes/skills.lock.json").read_text())
    path = tmp_path / "release.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, path, manifest


def test_release_maps_all_operations_and_has_document_only_composition(copied_release):
    root, path, _ = copied_release
    release = SkillRelease(root, path)
    union = {op for entry in release.entries.values() for op in entry["allowed_operations"]}
    from hermes_runtime.model_step_bridge import MODEL_STEP_TOOLS
    from workflow_runtime.catalog import OPERATIONS

    assert union == {"mcp__scat__" + name for name in ALL_READ_ONLY_TOOLS} | MODEL_STEP_TOOLS | set(
        OPERATIONS
    ) | {"scat_task_prepare", "scat_workflow_prepare"}
    assert not release.entries["scat-investigation"]["allowed_operations"]
    assert len(release.entries["scat-investigation"]["requires_skills"]) == 4
    assert not any("/scripts/" in name for name in release.files)


@pytest.mark.parametrize(
    "failure", ["missing", "version", "self-cycle", "cycle", "contract", "dependency-type"]
)
def test_invalid_composition_release_is_rejected(copied_release, failure):
    root, path, manifest = copied_release
    parent = next(x for x in manifest["skills"] if x["name"] == "scat-investigation")
    if failure == "missing":
        parent["requires_skills"]["absent"] = "0.2.0"
    if failure == "version":
        parent["requires_skills"]["scat-flight-query"] = "other"
    if failure == "self-cycle":
        parent["requires_skills"]["scat-investigation"] = "0.2.0"
    if failure == "cycle":
        manifest["skills"][0]["requires_skills"] = {"scat-investigation": "0.2.0"}
    if failure == "contract":
        parent["contract"]["inputs"] = ["../private"]
    if failure == "dependency-type":
        parent["requires_skills"] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(SkillPolicyError):
        SkillRelease(root, path)


def test_parent_does_not_inherit_child_permissions_and_list_exposes_contract(copied_release):
    root, path, _ = copied_release
    release = SkillRelease(root, path)
    folder = release.stage(root / "home")
    native = SimpleNamespace(
        skill_search_dirs=lambda: ([(1, folder)], folder),
        skills_list=lambda **kw: json.dumps(
            {"success": True, "skills": [{"name": n} for n in release.entries]}
        ),
        skill_view=lambda name, file_path, **kw: json.dumps(
            {
                "success": True,
                "content": (folder / name / (file_path or "SKILL.md")).read_text(encoding="utf-8"),
            }
        ),
    )
    bridge = NativeSkillBridge(release, folder, native, root / "trace.sqlite")
    bridge.bind("n", "scope", "run")
    listing = json.loads(bridge.handler("skills_list")({}, session_id="n"))
    assert (
        len(
            next(x for x in listing["skills"] if x["name"] == "scat-investigation")[
                "requires_skills"
            ]
        )
        == 4
    )
    guard = SkillOperationGuard(bridge, {"mcp__scat__" + x for x in ALL_READ_ONLY_TOOLS}, set())
    calls = []

    def dispatch(name, args, **kw):
        calls.append(name)
        return json.dumps({"status": "ok", "data": {}})

    def load(name):
        assert json.loads(bridge.handler("skill_view")({"name": name}, session_id="n"))["success"]

    query = "mcp__scat__search_scat_flights"
    track = "mcp__scat__get_scat_track_points"
    load("scat-investigation")
    assert (
        json.loads(guard.invoke(dispatch, query, {}, session_id="n"))["error"]
        == "skill_operation_not_allowed"
    )
    assert not calls
    load("scat-flight-query")
    assert json.loads(guard.invoke(dispatch, query, {}, session_id="n"))["status"] == "ok"
    assert (
        json.loads(guard.invoke(dispatch, track, {}, session_id="n"))["error"]
        == "skill_operation_not_allowed"
    )
    load("scat-trajectory-analysis")
    assert json.loads(guard.invoke(dispatch, track, {}, session_id="n"))["status"] == "ok"
    assert calls == [query, track]


def test_expanded_profile_keeps_legacy_five_tools():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "composition_profile", root / "scripts/run_hermes_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kwargs = {
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "with_mcp": True,
        "synthetic_mcp": True,
    }
    legacy = module.profile_config(**kwargs)
    new = module.profile_config(**kwargs, with_skills=True)
    assert len(legacy["mcp_servers"]["scat"]["tools"]["include"]) == 5
    assert set(new["mcp_servers"]["scat"]["tools"]["include"]) == set(ALL_READ_ONLY_TOOLS)
    assert "--skill-tools" not in legacy["mcp_servers"]["scat"]["args"]
    assert "--skill-tools" in new["mcp_servers"]["scat"]["args"]
    assert new["agent"]["max_turns"] == 16
