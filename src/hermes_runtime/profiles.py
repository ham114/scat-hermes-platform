"""Project-owned native profile definitions and safe metadata; no provider calls."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_runtime.skills import checked_path, digest, yaml_load

ROLES = {"scat-main": "main", "scat-worker": "worker", "scat-author": "author"}
DIRECTORIES = (
    "memories",
    "sessions",
    "skills",
    "cron",
    "logs",
    "workspace",
    "skins",
    "plans",
    "home",
)
DEFINITIONS = ("config.yaml", "SOUL.md", "operations.md")


class ProfileError(ValueError):
    pass


def profile_root(root, value=None):
    root = Path(root).resolve()
    return checked_path(
        root, value or os.environ.get("SCAT_HERMES_PROFILE_ROOT", "var/hermes/managed")
    )


def profile_home(root, name, base=None):
    if name not in ROLES:
        raise ProfileError("unknown_managed_profile")
    home = profile_root(root, base) / "profiles" / name
    return checked_path(Path(root).resolve(), home.relative_to(Path(root).resolve()).as_posix())


def validate_settings(config):
    if not isinstance(config, dict) or set(config) != {"model", "agent", "memory"}:
        raise ProfileError("invalid_profile_settings")
    model, agent, memory = (config[key] for key in ("model", "agent", "memory"))
    if not all(isinstance(value, dict) for value in (model, agent, memory)):
        raise ProfileError("invalid_profile_settings")
    if (
        set(model) != {"provider", "default", "base_url"}
        or model["provider"] != "deepseek"
        or model["base_url"] != "https://api.deepseek.com"
        or not isinstance(model["default"], str)
        or not 1 <= len(model["default"]) <= 100
    ):
        raise ProfileError("unsupported_profile_model")
    if (
        set(agent) != {"temperature", "max_turns"}
        or type(agent["temperature"]) not in {int, float}
        or not 0 <= agent["temperature"] <= 2
        or type(agent["max_turns"]) is not int
        or not 1 <= agent["max_turns"] <= 48
    ):
        raise ProfileError("invalid_profile_generation")
    if set(memory) != {"memory_enabled", "user_profile_enabled"} or any(
        type(value) is not bool for value in memory.values()
    ):
        raise ProfileError("invalid_profile_memory")
    return config


def settings(root, name, *, base=None, template=False):
    if name not in ROLES:
        raise ProfileError("unknown_managed_profile")
    path = (
        Path(root).resolve() / "config/hermes-profiles" / name / "config.yaml"
        if template
        else profile_home(root, name, base) / "config.yaml"
    )
    checked_path(Path(root).resolve(), path.relative_to(Path(root).resolve()).as_posix())
    if path.stat().st_size > 16384:
        raise ProfileError("profile_settings_too_large")
    data = yaml_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError("invalid_profile_settings")
    selected = {
        "model": data.get("model"),
        "agent": {k: data.get("agent", {}).get(k) for k in ("temperature", "max_turns")},
        "memory": data.get("memory"),
    }
    return validate_settings(selected)


def metadata(root, name, *, base=None):
    home = profile_home(root, name, base)
    config = settings(root, name, base=base)
    from dotenv import dotenv_values

    env_file = checked_path(
        Path(root).resolve(), (home / ".env").relative_to(Path(root).resolve()).as_posix()
    )
    configured = (
        bool(dotenv_values(env_file).get("DEEPSEEK_API_KEY")) if env_file.exists() else False
    )
    return {
        "name": name,
        "role": ROLES[name],
        "home": home.relative_to(Path(root).resolve()).as_posix(),
        "settings": config,
        "settings_sha256": digest(json.dumps(config, sort_keys=True).encode()),
        "config_sha256": digest((home / "config.yaml").read_bytes()),
        "key_configured": configured,
        "directories_valid": all(
            (home / p).is_dir() and not (home / p).is_symlink() for p in DIRECTORIES
        ),
        "state_db_exists": (home / "state.db").is_file(),
    }


def runtime_settings(root, name, *, base=None):
    config = settings(root, name, base=base)
    home = profile_home(root, name, base)
    return home, config


def apply_settings(config, specification, budget=None):
    config["model"] = dict(specification["model"])
    config["agent"]["max_turns"] = min(
        specification["agent"]["max_turns"], budget.max_turns if budget else 48
    )
    config["agent"]["temperature"] = specification["agent"]["temperature"]
    config["memory"] = dict(specification["memory"])
    return config
