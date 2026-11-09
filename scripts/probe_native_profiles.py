"""Native create/discover/state/credential/lock acceptance with no model requests."""

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor/hermes-agent")]


def main():
    from hermes_runtime.native_profiles import initialize, prepare_runtime, update_settings
    from hermes_runtime.profiles import ROLES, metadata, profile_home

    temporary = ROOT / ".tmp/profiles" / ("native-" + uuid4().hex[:8])
    base = (temporary / "managed").relative_to(ROOT).as_posix()
    temporary.mkdir(parents=True)
    os.environ.update(
        TMPDIR=str(temporary),
        TMP=str(temporary),
        TEMP=str(temporary),
        HERMES_DISABLE_LAZY_INSTALLS="1",
    )
    locks = []
    try:
        report = initialize(ROOT, base)
        assert report["native_discovery"] and len(report["profiles"]) == 3
        assert initialize(ROOT, base)["native_discovery"]
        for name in ROLES:
            home = profile_home(ROOT, name, base)
            assert (home / "SOUL.md").is_file() and (home / "AGENTS.md").is_file()
            assert (home / ".env").stat().st_mode & 0o077 == 0
            with sqlite3.connect(home / "state.db") as db:
                assert (
                    db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[
                        0
                    ]
                    > 4
                )
            (home / ".env").write_text("DEEPSEEK_API_KEY=synthetic-provider-" + name + "\n")
            instance = temporary / "instances" / name
            os.environ["HERMES_HOME"] = str(instance)
            lock = prepare_runtime(ROOT, name, instance, base=base)
            locks.append(lock)
            assert (instance / ".env").read_text().count("synthetic-provider-" + name) == 1
            assert (instance / "state.db").read_bytes() != b""
            try:
                prepare_runtime(ROOT, name, instance, base=base)
                raise AssertionError("duplicate_runtime_admitted")
            except ValueError:
                pass
        before = metadata(ROOT, "scat-main", base=base)
        changed = update_settings(
            ROOT, "scat-main", {"temperature": 0.7}, before["config_sha256"], base
        )
        assert changed["settings"]["agent"]["temperature"] == 0.7
        result = {
            "status": "passed",
            "native_profiles": 3,
            "native_state_schema": True,
            "credential_isolation": True,
            "standard_layout": True,
            "duplicate_runtime_rejected": True,
            "configuration_update": True,
            "model_calls": 0,
        }
    finally:
        for lock in locks:
            lock.close()
        assert (
            temporary.resolve().parent == (ROOT / ".tmp/profiles").resolve()
            and not temporary.is_symlink()
        )
        for path in temporary.rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
        shutil.rmtree(temporary)
    result["temporary_removed"] = True
    (ROOT / "docs/validation/20261024-profiles-native.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    if sys.version_info[:2] != (3, 14) or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing scat-hermes Anaconda")
    main()
