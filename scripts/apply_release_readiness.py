"""Apply/rollback the frozen CPU candidate behind an already-active public maintenance gate."""

import argparse
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import deploy_forecast_contract as prior
import deploy_release_readiness as deploy

ROOT, OUT, DEPLOY = deploy.ROOT, deploy.OUT, deploy.DEPLOY


def point(plan, rollback=False):
    store = deploy.skill_store()
    target = plan["old_skill"]["sha"] if rollback else plan["new_skill_sha256"]
    store.load(target)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT sha,generation FROM active WHERE id=1").fetchone()
        assert current["sha"] in [plan["old_skill"]["sha"], plan["new_skill_sha256"]]
        if current["sha"] != target:
            db.execute("UPDATE active SET sha=?,generation=generation+1 WHERE id=1", (target,))
            db.execute(
                "INSERT INTO release_events(at,actor,previous,target,preview) VALUES(unixepoch(),?,?,?,?)",
                (
                    "release-readiness-operator",
                    current["sha"],
                    target,
                    "readiness-" + ("rollback-" if rollback else "") + plan["release"],
                ),
            )
        db.commit()


def rollback(plan, folder):
    prior.idle()
    point(plan, True)
    deploy.run(
        deploy.compose(folder / "rollback-compose.json")
        + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *plan["services"]],
        timeout=240,
    )
    (DEPLOY / "active-release").write_text(plan["previous"])
    deploy.save(
        OUT / "deployment-rollback.json",
        {"status": "rolled_back", "release": plan["previous"], "server_rebooted": False},
    )


def apply():
    plan = json.loads(deploy.plan_file().read_text())
    folder = DEPLOY / "releases" / plan["release"]
    assert not (OUT / "deployment-result.json").exists()
    assert Path("/var/lib/scat-hermes-portal/maintenance").exists(), "public_gate_required"
    assert (DEPLOY / "active-release").read_text().strip() == plan["previous"]
    assert re.search(r"(?m)^1521 passed, 2 skipped,", (OUT / "backend-full-final.log").read_text())
    assert json.loads((OUT / "cluster-smoke.json").read_text())["status"] == "passed", (
        "isolated_cluster_required"
    )
    assert deploy.sha(Path("/proc/sys/kernel/random/boot_id")) == plan["boot_sha256"]
    assert deploy.sha(ROOT / "var/gpu/production.json") == plan["production_gpu_sha256"]
    for name, old in plan["before"].items():
        assert deploy.metadata(name) == old
    for path, expected in plan["source_hashes"].items():
        assert deploy.sha(folder / "patch" / path) == expected
    prior.idle()
    scheduler = subprocess.run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"],
        capture_output=True, text=True, check=False, timeout=15,
    ).stdout.strip()
    assert scheduler in {"active", "inactive"}, "scheduler_state_requires_review"
    backup = OUT / "private-before-switch" / plan["release"]
    backup.mkdir(parents=True, exist_ok=False)
    backup.chmod(0o700)
    try:
        deploy.run(["systemctl", "--user", "stop", "scat-auto-skill-learning.service"])
        deploy.run(
            deploy.compose(folder / "rollback-compose.json")
            + ["stop", "--timeout", "30", "platform"]
        )
        prior.idle()
        deploy.run(
            deploy.compose(folder / "rollback-compose.json")
            + ["stop", "--timeout", "30", *[n for n in plan["services"] if n != "platform"]]
        )
        prior.idle()
        for relative in [
            "var/deploy/h7b/shared/state",
            "var/deploy/h7b/shared/skills",
            "var/deploy/h7b/shared/homes",
            "var/autonomy/learning",
            "var/autonomy/memory",
        ]:
            for path in (ROOT / relative).rglob("*"):
                if path.suffix not in [".sqlite", ".db"] or path.is_symlink():
                    continue
                dest = backup / path.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with (
                    sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as src,
                    sqlite3.connect(dest) as dst,
                ):
                    src.backup(dst)
                    assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                dest.chmod(0o600)
        point(plan)
        for tier in [
            ["actions", "author-api", "hermes-worker"],
            ["hermes-main", "hermes-author"],
            ["platform"],
            ["worker"],
        ]:
            deploy.run(
                deploy.compose(folder / "compose.yaml")
                + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *tier],
                timeout=240,
            )
        assert deploy.sha(Path("/proc/sys/kernel/random/boot_id")) == plan["boot_sha256"]
        assert deploy.sha(ROOT / "var/gpu/production.json") == plan["production_gpu_sha256"]
        health = {n: deploy.metadata(n)["health"] for n in plan["services"]}
        assert all(v == "healthy" for v in health.values())
        (DEPLOY / "active-release").write_text(plan["release"])
        deploy.save(
            OUT / "deployment-result.json",
            {
                "status": "applied_behind_gate",
                "release": plan["release"],
                "health": health,
                "server_rebooted": False,
                "production_gpu_changed": False,
            },
        )
        print(
            json.dumps(
                {"status": "applied_behind_gate", "release": plan["release"], "health": health}
            )
        )
    except Exception:
        rollback(plan, folder)
        raise
    finally:
        if scheduler == "active":
            deploy.run(["systemctl", "--user", "start", "scat-auto-skill-learning.service"])


if __name__ == "__main__":
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["apply", "rollback"])
    a = p.parse_args()
    if a.action == "apply":
        apply()
    else:
        plan = json.loads(deploy.plan_file().read_text())
        rollback(plan, DEPLOY / "releases" / plan["release"])
