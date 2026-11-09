import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hermes_runtime.author_bridge import AuthorBridge
from hermes_runtime.entrypoint import resolve_profile
from hermes_runtime.native_profiles import update_settings
from hermes_runtime.profiles import (
    DIRECTORIES,
    ROLES,
    ProfileError,
    apply_settings,
    metadata,
    profile_home,
    profile_root,
    settings,
    validate_settings,
)
from hermes_runtime.skill_budget import BudgetExhausted

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.delenv("SCAT_HERMES_PROFILE_ROOT", raising=False)
    for name in ROLES:
        home = profile_home(tmp_path, name)
        for directory in DIRECTORIES:
            (home / directory).mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(json.dumps(settings(ROOT, name, template=True)))
        (home / ".env").write_text("DEEPSEEK_API_KEY=private-test-credential-" + name)
        (home / ".env").chmod(0o600)
        sqlite3.connect(home / "state.db").close()
    return tmp_path


@pytest.mark.parametrize("name", list(ROLES))
def test_metadata_and_role_settings_are_redacted(profiles, name):
    info = metadata(profiles, name)
    assert info["key_configured"] and info["directories_valid"]
    assert "private-test-credential" not in json.dumps(info)
    assert info["role"] == ROLES[name]


@pytest.mark.parametrize("value", ["../outside", "/tmp/no", "a/../../b", "C:/outside", "a\\b"])
def test_profile_root_cannot_escape_workspace(profiles, value):
    with pytest.raises(ValueError):
        profile_root(profiles, value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", -1),
        ("temperature", 3),
        ("temperature", float("nan")),
        ("temperature", True),
        ("max_turns", True),
        ("max_turns", 0),
        ("max_turns", 100),
    ],
)
def test_generation_settings_reject_unbounded_values(field, value):
    config = settings(ROOT, "scat-main", template=True)
    config["agent"][field] = value
    with pytest.raises(ProfileError):
        validate_settings(config)


def test_profile_update_uses_precondition_and_never_overwrites_env(profiles):
    before = metadata(profiles, "scat-main")
    key_file = profile_home(profiles, "scat-main") / ".env"
    secret = key_file.read_bytes()
    after = update_settings(profiles, "scat-main", {"temperature": 0.6}, before["config_sha256"])
    assert after["settings"]["agent"]["temperature"] == 0.6
    assert key_file.read_bytes() == secret
    with pytest.raises(ProfileError, match="configuration_conflict"):
        update_settings(profiles, "scat-main", {"model": "deepseek-flash"}, before["config_sha256"])
    with pytest.raises(ProfileError, match="not_editable"):
        update_settings(profiles, "scat-main", {"api_key": "bad"}, after["config_sha256"])


def test_active_profile_refuses_configuration_mutation(profiles):
    import fcntl

    home = profile_home(profiles, "scat-main")
    with (home / ".runtime.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ProfileError, match="already_active"):
            update_settings(
                profiles,
                "scat-main",
                {"temperature": 0.6},
                metadata(profiles, "scat-main")["config_sha256"],
            )


def test_profile_tuning_keeps_tool_policy_and_budget_ceiling():
    specification = settings(ROOT, "scat-worker", template=True)
    result = apply_settings(
        {"platform_toolsets": {"api_server": ["approved-only"]}, "agent": {}},
        specification,
        SimpleNamespace(max_turns=8),
    )
    assert result["agent"]["max_turns"] == 8
    assert result["platform_toolsets"]["api_server"] == ["approved-only"]


def test_author_profile_does_not_inherit_business_defaults():
    fields = {
        k: False
        for k in (
            "with_mcp",
            "with_skills",
            "with_workflows",
            "with_model_tasks",
            "background_job",
            "legacy_h5",
        )
    }
    args = resolve_profile(SimpleNamespace(profile="scat-author", **fields))
    assert not any(getattr(args, field) for field in fields)
    with pytest.raises(ValueError):
        resolve_profile(SimpleNamespace(profile="scat-author", **(fields | {"with_mcp": True})))


@pytest.fixture
def bridge(profiles):
    path = profiles / "author.key"
    path.write_text("a" * 40)
    path.chmod(0o600)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"draft": "1" * 32, "revision": 1})

    instance = AuthorBridge(
        profiles, "http://127.0.0.1:18112", "author.key", transport=httpx.MockTransport(handler)
    )
    instance.bind("native", "owner-thread", "run")
    return instance, calls


def test_draft_tool_uses_author_credential_and_no_release_credential(bridge):
    instance, calls = bridge
    result = json.loads(
        instance.handler("skill_draft")(
            {"operation": "create", "content": {"changes": []}}, session_id="native"
        )
    )
    assert result["revision"] == 1 and len(calls) == 1
    assert calls[0].url.path == "/v1/skills/drafts"
    assert "x-scat-author-key" in calls[0].headers and "x-scat-release-key" not in calls[0].headers
    assert "a" * 40 not in json.dumps(result)


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "publish"},
        {"operation": "confirm"},
        {"operation": "rollback"},
        {"operation": "read", "identity": "../releases"},
        {"operation": "create", "url": "http://elsewhere"},
    ],
)
def test_author_tools_cannot_reach_release_or_arbitrary_routes(bridge, args):
    instance, calls = bridge
    assert not json.loads(instance.handler("skill_draft")(args, session_id="native"))["success"]
    assert not calls


def test_profile_manage_model_surface_is_readonly(bridge):
    instance, calls = bridge
    result = instance.handler("profile_manage")({"action": "list"}, session_id="native")
    assert len(json.loads(result)["profiles"]) == 3 and "private-test-credential" not in result
    assert not json.loads(
        instance.handler("profile_manage")(
            {"action": "set", "profile": "scat-main"}, session_id="native"
        )
    )["success"]
    assert not calls


def test_unbound_and_exhausted_author_cannot_dispatch(bridge):
    instance, calls = bridge
    assert not json.loads(
        instance.handler("skill_draft")({"operation": "list"}, session_id="forged")
    )["success"]

    class Budget:
        def admit_tool(self):
            raise BudgetExhausted("budget_exhausted:max_tool_calls")

    instance._budgets["native"] = Budget()
    assert (
        json.loads(instance.handler("skill_draft")({"operation": "list"}, session_id="native"))[
            "error"
        ]
        == "budget_exhausted:max_tool_calls"
    )
    assert not calls
