"""Verify live/backup invariants and remove only this task's finished test directories."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"


def main():
    audit = json.loads((OUT / "acceptance-audit.json").read_text())
    assert audit["status"] == "passed" and audit["actual_gpu_posts"] == 2
    assert json.loads((OUT / "backend-full-status.json").read_text())["exit_code"] == 0
    assert "1492 passed, 2 skipped" in (OUT / "backend-full.log").read_text()
    before = json.loads((OUT / "audit-before.json").read_text())
    assert (
        hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
        == before["boot_sha256"]
    )
    health = {}
    for name, original in before["containers"].items():
        row = json.loads(
            subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-" + name + "-1"])
        )[0]
        health[name] = row["State"]["Health"]["Status"]
        assert health[name] == "healthy"
        if name in {"author-api", "hermes-author"}:
            assert row["Id"] == original["id"]
    gpu = json.loads(subprocess.check_output(["docker", "inspect", "scat-gbar-production-gbar-1"]))[
        0
    ]
    assert gpu["Id"] == before["gpu_container"]["id"]
    backups = (
        list((OUT / "state-before-switch").rglob("*.sqlite"))
        + list((OUT / "state-before-switch").rglob("*.db"))
        + list(OUT.glob("gpu*sqlite"))
    )
    assert backups
    for p in backups:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        with sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    initial = json.loads((OUT / "initial-deployment-plan.json").read_text())
    relative = Path("var/deploy/h7b/shared/state/tasks.sqlite")
    with sqlite3.connect(
        "file:" + str(OUT / "state-before-switch" / initial["release"] / relative) + "?mode=ro",
        uri=True,
    ) as db:
        old_tasks = {r[0] for r in db.execute("SELECT task_id FROM model_tasks")}
    with sqlite3.connect("file:" + str(ROOT / relative) + "?mode=ro", uri=True) as db:
        new_tasks = {r[0] for r in db.execute("SELECT task_id FROM model_tasks")} - old_tasks
        assert new_tasks == {case["task_id"] for case in audit["cases"].values()}
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM gpu_requests WHERE state IN ('running','unknown')"
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*),sum(calls) FROM gpu_requests").fetchone() == (6, 6)
    scheduler = subprocess.check_output(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"], text=True
    ).strip()
    assert scheduler == "active"
    for pid in [3052982, 3071762, 3088840]:
        path = Path("/proc") / str(pid) / "cmdline"
        assert not path.exists() or b".tmp/gbar-live" not in path.read_bytes()
    temporary = ROOT / ".tmp/gbar-live"
    assert not temporary.is_symlink() and temporary.resolve() == Path(
        "/home/h3c/project/python/scat-hermes-platform/.tmp/gbar-live"
    )
    if temporary.exists():
        shutil.rmtree(temporary)
    # The first failed test command used pytest's default directory; remove only its exact owned run.
    legacy = Path("/tmp/pytest-of-h3c/pytest-45")
    legacy_removed = False
    if legacy.exists():
        assert (
            not legacy.is_symlink()
            and legacy.resolve() == legacy
            and legacy.stat().st_uid == os.getuid()
        )
        shutil.rmtree(legacy)
        legacy_removed = True
        link = legacy.parent / "pytest-current"
        if link.is_symlink() and link.resolve() == legacy:
            link.unlink()
    report = {
        "status": "passed",
        "release": audit["release"],
        "health": health,
        "server_not_rebooted": True,
        "gpu_container_not_restarted": True,
        "author_containers_unchanged": True,
        "actual_gpu_posts": 2,
        "all_new_calls_one": True,
        "new_tasks": len(new_tasks),
        "private_backup_quick_checks": len(backups),
        "scheduler": scheduler,
        "owned_temporary_directory_removed": not temporary.exists(),
        "initial_pytest_temporary_directory_removed": legacy_removed,
        "raw_inputs_results_and_private_backups_retained_server_side": True,
    }
    (OUT / "closeout-server.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
