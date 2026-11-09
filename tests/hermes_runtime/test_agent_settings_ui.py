import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from hermes_runtime.agent_settings import AgentSettingsStore
from hermes_runtime.daily_budget import DailyBudget
from hermes_runtime.profiles import DIRECTORIES, ROLES
from service.agent_settings_api import install_settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    (tmp_path / "config").mkdir()
    for name in ROLES:
        home = tmp_path / "managed/profiles" / name
        home.mkdir(parents=True)
        shutil.copyfile(
            ROOT / "config/hermes-profiles" / name / "config.yaml", home / "config.yaml"
        )
        for folder in DIRECTORIES:
            (home / folder).mkdir()
        (home / ".env").write_text("DEEPSEEK_API_KEY=private-test-value")
        (home / "state.db").touch()
    shutil.copyfile(ROOT / "config/autonomy.example.json", tmp_path / "config/autonomy.json")
    shutil.copyfile(ROOT / "config/skills-policy.yaml", tmp_path / "config/policy.yaml")
    shutil.copyfile(ROOT / "config/agent-tools.json", tmp_path / "config/tools.json")
    (tmp_path / "config/proxy.json").write_text(
        json.dumps(
            dict.fromkeys(
                ["daily_requests", "daily_units", "lifetime_requests", "lifetime_units"], 100
            )
        )
    )
    DailyBudget(tmp_path / "usage.sqlite")
    manifest = {
        "profiles": dict.fromkeys(ROLES, "managed"),
        "autonomy": "config/autonomy.json",
        "proxy": "config/proxy.json",
        "policy": "config/policy.yaml",
        "tools": "config/tools.json",
        "usage": "usage.sqlite",
        "gates": {},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return AgentSettingsStore(tmp_path, "manifest.json")


def test_snapshot_secrets_and_pending_not_effective(store):
    snapshot = store.snapshot("admin")
    assert not snapshot["runtime_observed"]
    assert "private-test-value" not in json.dumps(snapshot)
    assert snapshot["usage"]["lifetime"]["reserved_units"] == 0
    assert all(p["key_configured"] for p in snapshot["profiles"])


def test_profile_preview_preserves_before_and_rejects_nested_tool_entries(store):
    old = store.read("scat-main")["value"]["agent"]["temperature"]
    before, after = store.validate("scat-main", {"temperature": 0.4})
    assert before["value"]["agent"]["temperature"] == old
    assert after["agent"]["temperature"] == 0.4
    with pytest.raises(ValueError):
        store.validate("tools", {"scat-main": [{}], "scat-worker": [], "scat-author": []})


def test_profile_config_symlink_rejected(store):
    path = store.target("scat-main")
    target = path.with_name("linked.yaml")
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symlink privilege unavailable on this host")
    with pytest.raises(ValueError):
        store.read("scat-main")


def test_manifest_changes_invalidate_request_replay(store):
    identity = str(uuid4())
    sha = store.read("learning")["config_sha256"]
    store.stage("admin", "learning", {"enabled": False}, sha, identity)
    manifest = json.loads(store.manifest.read_text())
    manifest["gates"] = {"changed": "path"}
    store.manifest.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        store.stage("admin", "learning", {"enabled": False}, sha, identity)


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("scat-main", {"temperature": 0.3}),
        ("scat-worker", {"max_turns": 8}),
        ("learning", {"enabled": False}),
        ("memory", {"context_chars": 500}),
        ("proxy", {"daily_requests": 50}),
        ("policy", {"readonly": {"max_tool_calls": 10}}),
        ("tools", {"scat-main": ["scat_workflows"], "scat-worker": [], "scat-author": []}),
    ],
)
def test_stage_idempotent_revision_owner_and_unchanged_source(store, section, changes):
    before = store.target(section).read_bytes()
    sha = store.read(section)["config_sha256"]
    identity = str(uuid4())
    a = store.stage("admin", section, changes, sha, identity)
    assert a["state"] == "pending_maintenance"
    assert store.stage("admin", section, changes, sha, identity) == a
    assert store.target(section).read_bytes() == before
    assert store.snapshot("other")["proposals"] == []
    with pytest.raises(ValueError):
        store.stage("other", section, changes, sha, identity)
    with pytest.raises(ValueError):
        store.stage("admin", section, changes, "0" * 64, str(uuid4()))


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("scat-main", {"model": "arbitrary-model"}),
        ("scat-main", {"max_turns": True}),
        ("learning", {"auto_publish": True}),
        ("learning", {"min_successes": 0}),
        ("memory", {"base_url": "https://outside"}),
        ("proxy", {"lifetime_units": None}),
        ("proxy", {"daily_requests": 101}),
        ("policy", {"readonly": {"max_tool_calls": 1000}}),
        ("tools", {"scat-main": ["terminal"], "scat-worker": [], "scat-author": []}),
    ],
)
def test_reject_authority_expansion(store, section, changes):
    with pytest.raises(ValueError):
        store.validate(section, changes)


def test_settings_http_independent_confirmation_and_no_apply(store, monkeypatch):
    monkeypatch.setenv("SCAT_AGENT_SETTINGS_MANIFEST", "manifest.json")

    def auth(x_scat_author_key: str = Header(default="")):
        if x_scat_author_key != "author":
            raise HTTPException(401)
        return "admin"

    def release(x_scat_release_key: str = Header(default="")):
        if x_scat_release_key != "release":
            raise HTTPException(403)

    app = FastAPI()
    install_settings(app, store.root, auth, release)
    with TestClient(app) as http:
        assert http.get("/v1/settings").status_code == 401
        headers = {"X-SCAT-Author-Key": "author"}
        body = {
            "section": "learning",
            "changes": {"enabled": False},
            "expected_sha256": store.read("learning")["config_sha256"],
            "request_id": str(uuid4()),
            "confirmed": True,
        }
        assert http.post("/v1/settings/preview", headers=headers, json=body).status_code == 200
        assert http.post("/v1/settings/proposals", headers=headers, json=body).status_code == 403
        headers["X-SCAT-Release-Key"] = "release"
        assert (
            http.post("/v1/settings/proposals", headers=headers, json=body).json()["state"]
            == "pending_maintenance"
        )
        assert http.post("/v1/settings/apply", headers=headers, json=body).status_code == 404
        assert store.read("learning")["value"]["enabled"]


@pytest.mark.skipif(
    __import__("os").name != "posix", reason="Production POSIX file locks and directory fsync"
)
def test_apply_gate_stale_and_crash_fence(store):
    sha = store.read("learning")["config_sha256"]
    identity = str(uuid4())
    store.stage("admin", "learning", {"enabled": False}, sha, identity)

    def denied(_):
        raise ValueError("active_work")

    with pytest.raises(ValueError):
        store.apply(identity, denied)
    assert store.read("learning")["value"]["enabled"]
    result = store.apply(identity, lambda _: None)
    assert result["state"] == "written_restart_required"
    assert not store.read("learning")["value"]["enabled"]
    assert (store.path.parent / (identity + ".before.json")).stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        store.apply(identity, lambda _: None)
