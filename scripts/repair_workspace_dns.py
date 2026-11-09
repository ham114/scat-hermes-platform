"""Review or apply DNS-only recreation of idle main/worker native containers."""

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["hermes-main", "hermes-worker"]
DNS = ["223.5.5.5", "119.29.29.29"]


def run(args, timeout=180):
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, check=True, timeout=timeout
    ).stdout


def main(apply=False):
    release = (ROOT / "var/deploy/autonomy/active-release").read_text().strip()
    original = ROOT / "var/deploy/autonomy/releases" / release / "compose.yaml"
    spec = json.loads(original.read_text())
    folder = ROOT / "var/validation/20261107-writable-live"
    folder.mkdir(parents=True, exist_ok=True)
    override = folder / "dns-override.json"
    override.write_text(
        json.dumps({"services": {name: {"dns": DNS} for name in SERVICES}}, indent=2) + "\n"
    )
    override.chmod(0o600)
    report = {
        "release": release,
        "services": SERVICES,
        "dns": DNS,
        "images_changed": False,
        "state_paths_changed": False,
        "host_dns_changed": False,
    }
    for name in SERVICES:
        actual = json.loads(run(["docker", "inspect", "scat-hermes-h7b-" + name + "-1"]))[0]
        if actual["Image"] != spec["services"][name]["image"]:
            raise ValueError("active_image_mismatch")
    (folder / "dns-plan.json").write_text(json.dumps(report, indent=2) + "\n")
    if not apply:
        print(json.dumps(report))
        return
    for path, query in [
        (
            "var/deploy/h7b/shared/state/tasks.sqlite",
            "SELECT count(*) FROM model_tasks WHERE state IN ('queued','claimed','unknown')",
        ),
        ("var/deploy/h7b/shared/state/bindings.sqlite", "SELECT count(*) FROM runtime_admissions"),
        (
            "var/deploy/h7b/shared/skills/catalog.sqlite",
            "SELECT count(*) FROM trials WHERE state='running'",
        ),
        (
            "var/autonomy/learning/state.sqlite",
            "SELECT count(*) FROM automatic_skill_jobs WHERE state IN ('reserved','authoring','unknown','trial_running')",
        ),
    ]:
        with sqlite3.connect("file:" + (ROOT / path).as_posix() + "?mode=ro", uri=True) as db:
            if db.execute(query).fetchone()[0]:
                raise ValueError("inflight_or_unknown_work_prevents_recreation")
    # Production state backup stays only on the server; never downloaded.
    backup = folder / "bindings-before-dns.sqlite"
    if backup.exists():
        raise ValueError("prior_dns_attempt_requires_inspection")
    with (
        sqlite3.connect(
            "file:"
            + (ROOT / "var/deploy/h7b/shared/state/bindings.sqlite").as_posix()
            + "?mode=ro",
            uri=True,
        ) as src,
        sqlite3.connect(backup) as dst,
    ):
        src.backup(dst)
    backup.chmod(0o600)
    base = ["docker", "compose", "--project-name", "scat-hermes-h7b", "-f", str(original)]
    scheduler = run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"]
    ).strip()
    try:
        run(["systemctl", "--user", "stop", "scat-auto-skill-learning.service"])
        # Recheck after scheduler stop to close the automatic-author admission window.
        with sqlite3.connect(
            "file:" + (ROOT / "var/autonomy/learning/state.sqlite").as_posix() + "?mode=ro",
            uri=True,
        ) as db:
            if db.execute(
                "SELECT count(*) FROM automatic_skill_jobs WHERE state IN ('reserved','authoring','unknown','trial_running')"
            ).fetchone()[0]:
                raise ValueError("learning_admission_race")
        run(
            [
                *base,
                "-f",
                str(override),
                "up",
                "-d",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                "120",
                *SERVICES,
            ]
        )
        for name in SERVICES:
            row = json.loads(run(["docker", "inspect", "scat-hermes-h7b-" + name + "-1"]))[0]
            assert (
                row["Image"] == spec["services"][name]["image"]
                and row["State"]["Health"]["Status"] == "healthy"
            )
            assert row["HostConfig"]["Dns"] == DNS
        report["applied"] = True
    except (ValueError, AssertionError, subprocess.SubprocessError):
        run([*base, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", *SERVICES])
        report["rolled_back"] = True
        raise
    finally:
        if scheduler == "active":
            run(["systemctl", "--user", "start", "scat-auto-skill-learning.service"])
        (folder / "dns-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    if not (Path(os.sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args().apply)
