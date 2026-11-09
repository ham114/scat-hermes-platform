"""Pinned native reader/guard concurrency probe with no model requests or effects."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor/hermes-agent")]


def run():
    from hermes_runtime.authoring_store import AuthoringStore
    from hermes_runtime.release_bridge import ReleaseRoutingBridge
    from hermes_runtime.skill_guard import SkillOperationGuard
    from hermes_runtime.skills import SkillRelease

    if sys.version_info[:2] != (3, 14) or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing native Anaconda required")
    home = ROOT / ".tmp/s7a" / ("native-routing-" + uuid4().hex[:8])
    home.mkdir(parents=True)
    os.environ.update(HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS="1")
    from tools import skills_tool as native

    store = AuthoringStore(ROOT, (home / "catalog.sqlite").relative_to(ROOT).as_posix())
    baseline = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
    baseline.verify_native_source(ROOT / "vendor/hermes-agent")
    bridge = ReleaseRoutingBridge(baseline, home, native, home / "trace.sqlite", store.releases)
    bridge.bind("old-session", "scope", "old-run")
    original_roots = native.skill_search_dirs()
    # Prepare an additional read-only skill with a valid version/contract and literal body.
    from hermes_runtime.skill_authoring import SECTIONS

    name = "scat-author-demo"
    body = (
        "---\nname: "
        + name
        + "\ndescription: 受控合成调查\n---\n"
        + "\n".join("## " + s + "\n执行已注册查询；无数据停止。\n" for s in SECTIONS)
    )
    content = {
        "changes": [
            {
                "name": name,
                "version": "1.0.0",
                "allowed_operations": ["mcp__scat__search_scat_flights"],
                "requires_skills": {},
                "contract": {
                    "version": 1,
                    "inputs": ["scat.request.v1"],
                    "outputs": ["scat.query-result.v1"],
                },
                "files": {"SKILL.md": body},
            }
        ]
    }
    row = store.write("probe", content)
    candidate = store.candidate(row)
    # Test-only pointer transaction: no fabricated trial is admitted by the author HTTP surface.
    with store.releases.connect() as db:
        db.execute("UPDATE active SET sha=?,generation=generation+1", (candidate.bundle_sha256,))
    bridge.bind("new-session", "scope", "new-run")
    assert all(child.catalog_cache is bridge.catalog_cache for child in bridge._bridges.values())
    guard = SkillOperationGuard(bridge, ["mcp__scat__search_scat_flights"], [])
    loads = bridge.handler("skill_view")
    assert not json.loads(loads({"name": name}, session_id="old-session"))["success"]
    assert json.loads(loads({"name": name}, session_id="new-session"))["success"]
    assert json.loads(loads({"name": "scat-flight-query"}, session_id="old-session"))["success"]

    def invoke(index):
        sid = "old-session" if index % 2 else "new-session"
        result = json.loads(bridge.handler("skills_list")({}, session_id=sid))
        assert result["success"]
        assert (name in {entry["name"] for entry in result["skills"]}) == (sid == "new-session")
        result = guard.invoke(
            lambda *args, **kwargs: '{"status":"ok"}',
            "mcp__scat__search_scat_flights",
            {"index": index},
            session_id=sid,
        )
        assert json.loads(result)["status"] == "ok"

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(invoke, range(24)))
    with store.releases.connect() as db:
        db.execute("UPDATE active SET sha=?,generation=generation+1", (baseline.bundle_sha256,))
    bridge.bind("rollback-session", "scope", "rollback-run")
    assert name not in {
        e["name"]
        for e in json.loads(bridge.handler("skills_list")({}, session_id="rollback-session"))[
            "skills"
        ]
    }
    assert json.loads(loads({"name": name}, session_id="new-session"))["success"]
    assert native.skill_search_dirs() == original_roots
    assert "numpy" not in sys.modules and "service.hermes_app" not in sys.modules
    report = {
        "status": "passed",
        "native_commit": baseline.manifest["upstream_commit"],
        "parallel_catalog_and_guard_calls": 24,
        "old_new_rollback_release_isolation": True,
        "upstream_roots_unchanged": True,
        "model_calls": 0,
        "effects": 0,
    }
    import shutil

    for path in home.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    assert home.resolve().parent == (ROOT / ".tmp/s7a").resolve() and not home.is_symlink()
    shutil.rmtree(home)
    report["temporary_home_removed"] = True
    target = ROOT / "docs/validation/20261024-S7A-native-routing.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    run()
