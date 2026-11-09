"""Exercise pinned native skill loading without any LLM calls or business effects."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(report):
    if sys.version_info[:2] != (3, 14) or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("dedicated_hermes_conda_required")
    home = ROOT / ".tmp/s1" / ("native-" + uuid4().hex[:12])
    home.mkdir(parents=True)
    try:
        os.environ.update(
            HERMES_HOME=str(home),
            HERMES_DISABLE_LAZY_INSTALLS="1",
            TMPDIR=str(home),
            TMP=str(home),
            TEMP=str(home),
        )
        (home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": []}}))
        sys.path.insert(0, str(ROOT / "vendor/hermes-agent"))
        from hermes_runtime.skills import SkillPolicyError, SkillRelease, register_native_skills

        release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
        bridge = register_native_skills(
            release, home, ROOT / "vendor/hermes-agent", home / "skill-loads.sqlite"
        )
        bridge.bind("native-policy-session", "native-policy-owner", "native-policy-run")
        native = bridge.native
        calls = []

        def forbidden(*args, **kwargs):
            calls.append("preprocess_or_dependency")
            raise AssertionError("unexpected_preprocessing_or_install")

        native._preprocess_skill = forbidden
        # Poison dependency entry point: none may be called during admitted native loads.
        import pm

        pm.ensure = forbidden
        listing = json.loads(bridge.handler("skills_list")({}, session_id="native-policy-session"))
        assert listing["success"] and {s["name"] for s in listing["skills"]} == set(release.entries)
        for name in release.entries:
            result = json.loads(
                bridge.handler("skill_view")({"name": name}, session_id="native-policy-session")
            )
            assert result["success"] and result["version"] == "0.1.0"
        linked = json.loads(
            bridge.handler("skill_view")(
                {"name": "scat-flight-query", "file_path": "references/query-contract.md"},
                session_id="native-policy-session",
            )
        )
        assert linked["success"]
        for args in [
            {"name": "terminal"},
            {"name": "../.env"},
            {"name": "scat-flight-query", "file_path": "../../.env"},
            {"name": "scat-flight-query", "preprocess": True},
        ]:
            assert not json.loads(
                bridge.handler("skill_view")(args, session_id="native-policy-session")
            )["success"]
        assert not json.loads(
            bridge.handler("skill_view")({"name": "scat-flight-query"}, session_id="other")
        )["success"]
        try:
            bridge.handler("skill_manage")
        except SkillPolicyError:
            pass
        else:
            raise AssertionError("manager_must_not_be_exposed")
        with sqlite3.connect(home / "skill-loads.sqlite") as db:
            count = db.execute("SELECT count(*) FROM skill_loads").fetchone()[0]
        assert count == 3 and not calls
        changed = home / "skills/scat-flight-query/SKILL.md"
        changed.chmod(0o600)
        changed.write_text("---\nname: scat-flight-query\ndeps: [ffmpeg]\n---\nchanged")
        assert not json.loads(
            bridge.handler("skill_view")(
                {"name": "scat-flight-query"}, session_id="native-policy-session"
            )
        )["success"]
        assert not calls
        report.update(
            status="passed",
            native_source_verified=True,
            native_catalog=sorted(release.entries),
            native_loads=count,
            preprocess_calls=0,
            dependency_install_calls=0,
            traversal_and_identity_rejected=True,
            changed_snapshot_rejected=True,
            skill_manage_exposed=False,
            bundle_sha256=release.bundle_sha256,
        )
    finally:
        shutil.rmtree(home)
        report["temporary_home_removed"] = not home.exists()


if __name__ == "__main__":
    report = {"status": "running"}
    try:
        main(report)
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AssertionError,
        ImportError,
    ) as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, ModuleNotFoundError):
            report["missing_module"] = exc.name
    path = ROOT / "docs/validation/20261023-S1-native.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0 if report["status"] == "passed" else 1)
