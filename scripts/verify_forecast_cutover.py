"""Sanitized before/after cutover evidence; private SQLite backups stay server-side."""

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from deploy_forecast_contract import container, idle, skill_update

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-forecast-contract"
NAMES = [
    "platform",
    "actions",
    "worker",
    "hermes-main",
    "hermes-worker",
    "author-api",
    "hermes-author",
]


def current_skill():
    code = "from pathlib import Path; import json; from hermes_runtime.release_store import ReleaseStore; s=ReleaseStore(Path.cwd()); db=s.connect(); print(json.dumps(list(db.execute('SELECT sha,generation FROM active WHERE id=1').fetchone()))); db.close()"
    return json.loads(
        subprocess.check_output(
            [
                "docker",
                "exec",
                "scat-hermes-h7b-hermes-main-1",
                "/opt/conda/envs/scat-hermes/bin/python",
                "-c",
                code,
            ]
        )
    )


def main():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    rows = {name: container(name) for name in NAMES}
    boot = hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
    health = {name: row["State"]["Health"]["Status"] for name, row in rows.items()}
    assert all(value == "healthy" for value in health.values())
    if sys.argv[1] == "before":
        idle()
        assert current_skill() == [
            plan["skill_change"]["previous_sha"],
            plan["skill_change"]["previous_generation"],
        ]
        skill_update(plan, "register")
        report = {
            "boot_sha256": boot,
            "containers": {
                name: {"id": row["Id"], "image": row["Image"]} for name, row in rows.items()
            },
            "skill": current_skill(),
            "health": health,
        }
        (OUT / "cutover-preflight.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"before": "passed", "candidate_skill_registered_not_active": True}))
        return
    previous = json.loads((OUT / "cutover-preflight.json").read_text())
    assert boot == previous["boot_sha256"]
    assert all(
        rows[name]["Id"] == previous["containers"][name]["id"]
        for name in ["author-api", "hermes-author"]
    )
    assert current_skill() == [
        plan["skill_change"]["target_sha"],
        plan["skill_change"]["previous_generation"]
        + int(plan["skill_change"]["target_sha"] != plan["skill_change"]["previous_sha"]),
    ]
    for name in ["platform", "hermes-main"]:
        for relative, expected in plan["source_hashes"].items():
            actual = subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "scat-hermes-h7b-" + name + "-1",
                    "sha256sum",
                    str(ROOT / relative),
                ],
                text=True,
            ).split()[0]
            assert actual == expected
    backup_root = OUT / "state-before-switch" / plan["release"]
    backups = list(backup_root.rglob("*.sqlite")) + list(backup_root.rglob("*.db"))
    assert backups
    for path in backups:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    scheduler = subprocess.check_output(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"], text=True
    ).strip()
    assert scheduler == "active"
    report = {
        "release": plan["release"],
        "server_not_rebooted": True,
        "health": health,
        "author_containers_unchanged": True,
        "skill": current_skill(),
        "source_files_matched_per_image": len(plan["source_hashes"]),
        "private_backups": len(backups),
        "backup_quick_checks": "ok",
        "scheduler": scheduler,
    }
    (OUT / "cutover-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
