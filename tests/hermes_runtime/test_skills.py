import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from hermes_runtime.skills import (
    PINNED_COMMIT,
    NativeSkillBridge,
    SkillPolicyError,
    SkillRelease,
    relative_path,
)


def make_release(root, *, body=None, extra_header="", operations=None):
    folder = root / "skills/test-query"
    folder.mkdir(parents=True, exist_ok=True)
    payload = (
        "---\nname: test-query\ndescription: query workflow\n"
        + extra_header
        + "---\n\n"
        + (body or "Call the registered query tool.")
    )
    (folder / "SKILL.md").write_text(payload, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": 1,
        "bundle_version": "test.1",
        "upstream_commit": PINNED_COMMIT,
        "skills": [
            {
                "name": "test-query",
                "version": "1.0.0",
                "allowed_operations": operations or ["query"],
                "files": {"SKILL.md": hashlib.sha256(payload.encode()).hexdigest()},
            }
        ],
    }
    path = root / "lock.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return SkillRelease(root, path)


class Native:
    SKILLS_LIST_SCHEMA: ClassVar[dict] = {"name": "skills_list"}
    SKILL_VIEW_SCHEMA: ClassVar[dict] = {"name": "skill_view"}

    def __init__(self, folder):
        self.folder, self.calls = folder, []

    def skill_search_dirs(self):
        return [(1, self.folder)], self.folder

    def skills_list(self, **kwargs):
        return json.dumps({"success": True, "skills": [{"name": "test-query"}]})

    def skill_view(self, name, *, file_path, preprocess, task_id):
        self.calls.append((name, file_path, preprocess, task_id))
        return json.dumps(
            {
                "success": True,
                "content": (self.folder / name / (file_path or "SKILL.md")).read_text(
                    encoding="utf-8"
                ),
            }
        )


@pytest.mark.parametrize(
    "path",
    [
        "../.env",
        "/etc/passwd",
        "C:/secret",
        "a\\b",
        "a/../b",
        "plugin:skill",
        "a//b",
        "./a",
        "",
        "a/.",
    ],
)
def test_paths_reject_cross_platform_escapes(path):
    with pytest.raises(SkillPolicyError):
        relative_path(path)


@pytest.mark.parametrize(
    "header", ["deps: [ffmpeg]\n", "deps: ffmpeg\n", "setup: execute\n", "name: different\n"]
)
def test_admission_rejects_dependency_installation_and_active_metadata(tmp_path, header):
    with pytest.raises(SkillPolicyError):
        make_release(tmp_path, extra_header=header)


def test_source_edits_do_not_change_an_admitted_running_snapshot(tmp_path):
    release = make_release(tmp_path)
    folder = release.stage(tmp_path / "home")
    (tmp_path / "skills/test-query/SKILL.md").write_text("changed", encoding="utf-8")
    release.verify_tree(folder)
    with pytest.raises(SkillPolicyError, match="digest"):
        SkillRelease(tmp_path, tmp_path / "lock.json")


def test_trusted_native_loading_disables_preprocessing_and_records_identity(tmp_path):
    release = make_release(tmp_path, body="Literal !`echo do-not-run` and {{ user_input }}")
    folder = release.stage(tmp_path / "home")
    native = Native(folder)
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    view = bridge.handler("skill_view")
    assert not json.loads(view({"name": "test-query"}, session_id="forged"))["success"]
    assert not native.calls
    bridge.bind("native-id", "trusted-owner-thread", "trusted-run")
    result = json.loads(view({"name": "test-query"}, session_id="native-id", task_id="forged"))
    assert result["success"] and "!`echo do-not-run`" in result["content"]
    assert native.calls == [("test-query", None, False, "native-id")]
    with sqlite3.connect(tmp_path / "trace.sqlite") as db:
        row = db.execute(
            "SELECT scope,run_id,skill_id,version,bundle_sha256 FROM skill_loads"
        ).fetchone()
    assert row == (
        "trusted-owner-thread",
        "trusted-run",
        "test-query",
        "1.0.0",
        release.bundle_sha256,
    )
    with pytest.raises(SkillPolicyError):
        bridge.bind("native-id", "another-owner", "other-run")
    with pytest.raises(SkillPolicyError):
        bridge.handler("skill_manage")


@pytest.mark.parametrize(
    "args",
    [
        {"name": "unknown"},
        {"name": "../test-query"},
        {"name": "plugin:test-query"},
        {"name": "test-query", "file_path": "../.env"},
        {"name": "test-query", "file_path": "references/unpublished.md"},
        {"name": "test-query", "preprocess": True},
        {"name": ["test-query"]},
    ],
)
def test_model_cannot_broaden_read_scope(tmp_path, args):
    release = make_release(tmp_path)
    folder = release.stage(tmp_path / "home")
    native = Native(folder)
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    bridge.bind("native", "scope", "run")
    assert not json.loads(bridge.handler("skill_view")(args, session_id="native"))["success"]
    assert native.calls == []


def test_snapshot_tamper_stops_before_native_loading(tmp_path):
    release = make_release(tmp_path)
    folder = release.stage(tmp_path / "home")
    native = Native(folder)
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    bridge.bind("native", "scope", "run")
    target = folder / "test-query/SKILL.md"
    target.chmod(0o600)
    target.write_text("---\ndeps: [forbidden]\n---", encoding="utf-8")
    result = json.loads(bridge.handler("skill_view")({"name": "test-query"}, session_id="native"))
    assert not result["success"] and native.calls == []


def test_extra_roots_and_catalog_entries_are_rejected(tmp_path):
    release = make_release(tmp_path)
    folder = release.stage(tmp_path / "home")
    native = Native(folder)
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    bridge.bind("native", "scope", "run")
    native.skills_list = lambda **kw: json.dumps({"success": True, "skills": [{"name": "other"}]})
    assert not json.loads(bridge.handler("skills_list")({}, session_id="native"))["success"]
    native.skill_search_dirs = lambda: ([(1, folder), (3, tmp_path / "outside")], folder)
    assert not json.loads(
        bridge.handler("skill_view")({"name": "test-query"}, session_id="native")
    )["success"]
    assert native.calls == []


def test_support_files_are_locked_and_native_content_is_checked(tmp_path):
    release = make_release(tmp_path)
    root = tmp_path / "skills/test-query"
    (root / "references").mkdir()
    (root / "references/query.md").write_text("contract", encoding="utf-8")
    manifest = json.loads((tmp_path / "lock.json").read_text())
    manifest["skills"][0]["files"]["references/query.md"] = hashlib.sha256(b"contract").hexdigest()
    (tmp_path / "lock.json").write_text(json.dumps(manifest))
    release = SkillRelease(tmp_path, tmp_path / "lock.json")
    folder = release.stage(tmp_path / "home")
    native = Native(folder)
    bridge = NativeSkillBridge(release, folder, native, tmp_path / "trace.sqlite")
    bridge.bind("n", "s", "r")
    result = json.loads(
        bridge.handler("skill_view")(
            {"name": "test-query", "file_path": "references/query.md"}, session_id="n"
        )
    )
    assert result["success"] and result["content"] == "contract"
    native.skill_view = lambda *a, **kw: json.dumps({"success": True, "content": "other content"})
    assert not json.loads(bridge.handler("skill_view")({"name": "test-query"}, session_id="n"))[
        "success"
    ]


def test_unlisted_files_are_not_implicitly_admitted(tmp_path):
    make_release(tmp_path)
    (tmp_path / "skills/secret.md").write_text("not published")
    with pytest.raises(SkillPolicyError):
        SkillRelease(tmp_path, tmp_path / "lock.json")


def test_optional_profile_has_exact_read_tools_and_preserves_legacy_defaults():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "s1_api_profile", root / "scripts/run_hermes_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "with_mcp": True,
        "synthetic_mcp": False,
        "with_model_tasks": True,
    }
    assert module.profile_config(**base)["platform_toolsets"]["api_server"] == [
        "scat",
        "scat_tasks",
    ]
    config = module.profile_config(**base, with_skills=True)
    assert config["platform_toolsets"]["api_server"] == [
        "scat",
        "scat_tasks",
        "scat_skills",
        "scat_model_steps",
    ]
    assert "skills" not in config["platform_toolsets"]["api_server"]
    assert config["skills"]["external_dirs"] == []
    assert config["agent"]["max_turns"] == 16


def test_checked_in_release_is_complete():
    root = Path(__file__).resolve().parents[2]
    release = SkillRelease(root, root / "services/hermes/skills.lock.json")
    assert set(release.entries) == {
        "scat-flight-query",
        "aviation-knowledge",
        "scat-data-quality",
        "scat-trajectory-analysis",
        "uav-flight-analysis",
        "scat-investigation",
        "scat-model-analysis",
        "scat-model-comparison",
        "scat-data-materialization",
        "scat-evaluation",
        "scat-experiment",
        "scat-report",
    }


def test_workflow_profile_routes_new_models_by_live_catalog():
    import runpy

    root = Path(__file__).resolve().parents[2]
    profile = runpy.run_path(str(root / "scripts/run_hermes_api.py"))["profile_config"]
    config = profile(
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        with_mcp=True,
        synthetic_mcp=True,
        with_model_tasks=True,
        with_skills=True,
        with_workflows=True,
    )
    policy = config["platform_hints"]["api_server"]["append"]
    assert "单模型与模型比较均加载 scat-model-comparison" in policy
    assert "仅注册 CPU" not in policy
    assert "模型任务使用 scat-model-analysis" not in policy
    assert "skill_required" in policy and "上一轮加载" in policy
    assert "绘制二维轨迹图是已支持的业务" in policy
    assert "本轮需有可读取的轨迹回执" in policy
    assert "不在回复或工具参数中抄写坐标数组" in policy


def test_gateway_precreated_empty_skills_directory_can_be_initialized(tmp_path):
    release = make_release(tmp_path)
    (tmp_path / "home/skills").mkdir(parents=True)
    folder = release.stage(tmp_path / "home")
    release.verify_tree(folder)


def test_partial_release_directory_is_never_silently_repaired(tmp_path):
    release = make_release(tmp_path)
    folder = tmp_path / "home/skills"
    folder.mkdir(parents=True)
    (folder / "unknown.md").write_text("unexpected")
    with pytest.raises(SkillPolicyError):
        release.stage(tmp_path / "home")
    assert not (folder / "test-query/SKILL.md").exists()
