from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "register_gbar_provenance.py"
SPEC = importlib.util.spec_from_file_location("register_gbar_provenance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_dotenv_update_replaces_keys_without_touching_secrets() -> None:
    original = "AUTH_SECRET=keep-me\nSCAT_GBAR_V23_SOURCE_URI=old://value\n"
    updated = MODULE._updated_dotenv(
        original,
        {
            "SCAT_GBAR_V23_SOURCE_URI": "project-internal://reviewed",
            "SCAT_GBAR_V23_CHECKPOINT_SHA256": "a" * 64,
        },
    )
    assert "AUTH_SECRET=keep-me" in updated
    assert updated.count("SCAT_GBAR_V23_SOURCE_URI=") == 1
    assert "project-internal://reviewed" in updated
    assert f"SCAT_GBAR_V23_CHECKPOINT_SHA256={'a' * 64}" in updated
