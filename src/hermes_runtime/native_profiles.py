"""Fixed native profile lifecycle. Run only in the existing Hermes Conda process."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from hermes_runtime.profiles import (
    DIRECTORIES,
    ROLES,
    ProfileError,
    metadata,
    profile_home,
    profile_root,
    settings,
    validate_settings,
)
from hermes_runtime.skills import checked_path, digest, yaml_load


def verify_native(root):
    lock = json.loads((root / "services/hermes/profiles.lock.json").read_text())
    for name, expected in lock["files"].items():
        if digest((root / "vendor/hermes-agent" / name).read_bytes()) != expected:
            raise ProfileError("native_profile_source_changed")


def atomic_text(path, text, *, mode=0o600):
    temporary = path.with_name(path.name + ".profile-write")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def seed_layout(root, home, name, *, reuse_key=False, source_home=None):
    template = root / "config/hermes-profiles" / name
    for folder in DIRECTORIES:
        checked_path(root, (home / folder).relative_to(root).as_posix()).mkdir(
            parents=True, exist_ok=True
        )
    for target, source in (("SOUL.md", "SOUL.md"), ("AGENTS.md", "operations.md")):
        if not (home / target).exists():
            atomic_text(
                home / target,
                (source_home / target if source_home else template / source).read_text(
                    encoding="utf-8"
                ),
            )
    workspace_instructions = home / "workspace/AGENTS.md"
    if not workspace_instructions.exists():
        atomic_text(workspace_instructions, (home / "AGENTS.md").read_text())
    for path in ("memories/MEMORY.md", "memories/USER.md"):
        if not (home / path).exists():
            atomic_text(home / path, "")
    from dotenv import dotenv_values

    key_file = home / ".env"
    values = (
        dotenv_values(source_home / ".env" if source_home else key_file)
        if (source_home or key_file.exists())
        else {}
    )
    if reuse_key and not values.get("DEEPSEEK_API_KEY"):
        values = {"DEEPSEEK_API_KEY": dotenv_values(root / ".env").get("DEEPSEEK_API_KEY", "")}
    key = values.get("DEEPSEEK_API_KEY", "") or ""
    if "\n" in key or "\r" in key or "'" in key:
        raise ProfileError("invalid_provider_credential")
    if (
        source_home
        or not key_file.exists()
        or (reuse_key and not dotenv_values(key_file).get("DEEPSEEK_API_KEY"))
    ):
        atomic_text(
            key_file,
            "# Private provider credential; never commit or print.\nDEEPSEEK_API_KEY='"
            + key
            + "'\n",
        )
    key_file.chmod(0o600)
    from hermes_state import SessionDB

    if not (home / "state.db").exists():
        database = SessionDB(home / "state.db")
        database.close()
    else:
        with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ProfileError("profile_state_corrupt")


def initialize(root, base=None, *, reuse_key=False):
    import fcntl

    root = Path(root).resolve()
    if base is None:
        import secrets

        secret_directory = checked_path(root, "var/secrets")
        secret_directory.mkdir(parents=True, exist_ok=True)
        for filename in ("skill-author.key", "skill-release.key"):
            path = checked_path(root, "var/secrets/" + filename)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if path.stat().st_mode & 0o077 or len(path.read_text().strip()) < 32:
                    raise ProfileError("private_service_credential_invalid") from None
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(secrets.token_urlsafe(48))
    base_path = profile_root(root, base)
    base_path.mkdir(parents=True, exist_ok=True)
    base_path.chmod(0o700)
    os.environ["HERMES_HOME"] = str(base_path)
    if not (base_path / "config.yaml").exists():
        atomic_text(
            base_path / "config.yaml",
            json.dumps(
                {
                    "model": {
                        "provider": "deepseek",
                        "default": "deepseek-flash",
                        "base_url": "https://api.deepseek.com",
                    }
                }
            ),
        )
    verify_native(root)
    from hermes_cli.profiles import create_profile, get_profile_dir, list_profiles

    if get_profile_dir("scat-main").resolve() != (base_path / "profiles/scat-main").resolve():
        raise ProfileError("native_profile_root_mismatch")
    with (base_path / ".manage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ROLES:
            home = profile_home(root, name, base)
            marker = home / ".scat-profile.json"
            if not home.exists():
                create_profile(
                    name,
                    no_alias=True,
                    no_skills=True,
                    description="SCAT " + ROLES[name],
                    soul=(root / "config/hermes-profiles" / name / "SOUL.md").read_text(),
                )
                atomic_text(
                    marker,
                    json.dumps(
                        {"schema_version": 1, "name": name, "role": ROLES[name], "native": True}
                    ),
                )
                atomic_text(
                    home / "config.yaml",
                    json.dumps(settings(root, name, template=True), ensure_ascii=False, indent=2),
                )
            elif not marker.exists():
                raise ProfileError("unmanaged_existing_profile_preserved")
            seed_layout(root, home, name, reuse_key=reuse_key)
        listed = {p.name for p in list_profiles()}
        if not set(ROLES) <= listed:
            raise ProfileError("native_profile_discovery_failed")
    return {
        "status": "ok",
        "native_discovery": True,
        "profiles": [metadata(root, name, base=base) for name in ROLES],
    }


def prepare_runtime(root, name, home, *, base=None):
    import fcntl

    root, home = Path(root).resolve(), Path(home)
    checked_path(root, home.relative_to(root).as_posix())
    canonical = profile_home(root, name, base)
    if not (canonical / ".scat-profile.json").is_file():
        raise ProfileError("managed_profile_not_initialized")
    home.mkdir(parents=True, exist_ok=True)
    # The caller retains this lock until its native HTTP service has stopped.
    lock = (home / ".runtime.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ProfileError("profile_runtime_already_active") from None
    try:
        seed_layout(root, home, name, source_home=canonical if home != canonical else None)
        if home != canonical and not (home / "profile.yaml").exists():
            atomic_text(
                home / "profile.yaml",
                json.dumps({"description": "SCAT isolated " + ROLES[name], "source_profile": name}),
            )
        return lock
    except BaseException:
        lock.close()
        raise


def update_settings(root, name, changes, expected, base=None):
    import fcntl

    home = profile_home(root, name, base)
    with (home / ".runtime.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProfileError("profile_runtime_already_active") from None
        if digest((home / "config.yaml").read_bytes()) != expected:
            raise ProfileError("profile_configuration_conflict")
        if set(changes) - {"model", "temperature", "max_turns"}:
            raise ProfileError("profile_setting_not_editable")
        config = yaml_load((home / "config.yaml").read_text())
        selected = settings(root, name, base=base)
        for field, value in changes.items():
            selected["model" if field == "model" else "agent"][
                "default" if field == "model" else field
            ] = value
        validate_settings(selected)
        for section in selected:
            config.setdefault(section, {}).update(selected[section])
        atomic_text(home / "config.yaml", json.dumps(config, ensure_ascii=False, indent=2))
    return metadata(root, name, base=base)
