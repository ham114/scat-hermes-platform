"""Publish receipt projections through a backed-up, idle-gated CPU-only release."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import deploy_forecast_contract as base

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/autonomy"
OUT = ROOT / "var/validation/20261108-business-parity"
FILES = [
    "src/agents/analysis_results.py",
    "src/application/readonly_display.py",
    "src/application/readonly_extensions.py",
]
SERVICES = ["platform", "actions", "worker"]


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage():
    previous = (DEPLOY / "active-release").read_text().strip()
    old = DEPLOY / "releases" / previous
    compose = json.loads((old / "compose.yaml").read_text())
    live = {n: base.container(n) for n in compose["services"]}
    for n in SERVICES:
        assert live[n]["Image"] == compose["services"][n]["image"]
        assert live[n]["State"]["Health"]["Status"] == "healthy"
    hashes = {p: sha(ROOT / p) for p in FILES}
    identity = hashlib.sha256(json.dumps([previous, hashes], sort_keys=True).encode()).hexdigest()[
        :12
    ]
    release = "20261108-parity-" + identity
    folder = DEPLOY / "releases" / release
    folder.mkdir(exist_ok=False)
    shutil.copyfile(old / "compose.yaml", folder / "rollback-compose.json")
    for p in FILES:
        dest = folder / "patch" / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / p, dest)
    assert shutil.disk_usage(ROOT).free > 10 * 1024**3
    baseline = live["platform"]["Image"]
    tag = "scat-parity-base:" + identity
    base.run(["docker", "image", "tag", baseline, tag])
    (folder / "Dockerfile").write_text(
        "FROM "
        + tag
        + "\n"
        + "".join("COPY --chown=1000:1000 patch/" + p + " " + str(ROOT / p) + "\n" for p in FILES)
    )
    target = "scat-business-parity:" + identity
    (OUT / "build-business.log").write_text(
        base.run(
            ["docker", "build", "--network=none", "--pull=false", "-t", target, str(folder)],
            timeout=240,
        )
    )
    image = base.run(["docker", "image", "inspect", target, "--format", "{{.Id}}"]).strip()
    for n in SERVICES:
        compose["services"][n]["image"] = image
    for filename in ["features.json", "images.lock.json", "source.lock.json"]:
        if (old / filename).exists():
            shutil.copyfile(old / filename, folder / filename)
    sources = json.loads((folder / "source.lock.json").read_text())
    sources.update(hashes)
    save(folder / "source.lock.json", sources)
    images = json.loads((folder / "images.lock.json").read_text())
    images["business"] = {"tag": target, "image_id": image, "base_image_id": baseline}
    save(folder / "images.lock.json", images)
    save(folder / "compose.yaml", compose)
    base.run(base.compose_command(folder / "compose.yaml") + ["config", "--quiet"])
    save(
        OUT / "deployment-plan.json",
        {
            "previous": previous,
            "release": release,
            "image": image,
            "source_hashes": hashes,
            "services": SERVICES,
            "before_ids": {n: c["Id"] for n, c in live.items()},
            "boot_sha256": sha(Path("/proc/sys/kernel/random/boot_id")),
            "gpu_registration_sha256": sha(ROOT / "var/gpu/production.json"),
            "skills_active_sha256": sha(ROOT / "var/deploy/h7b/shared/skills/catalog.sqlite"),
            "new_gpu_calls": 0,
            "skill_change": False,
        },
    )
    print(json.dumps({"built": release, "applied": False}))


def apply():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    folder = DEPLOY / "releases" / plan["release"]
    assert not (OUT / "deployment-result.json").exists()
    assert (DEPLOY / "active-release").read_text().strip() == plan["previous"]
    assert sha(Path("/proc/sys/kernel/random/boot_id")) == plan["boot_sha256"]
    assert sha(ROOT / "var/gpu/production.json") == plan["gpu_registration_sha256"]
    for p, expected in plan["source_hashes"].items():
        assert sha(folder / "patch" / p) == expected
    for n, expected in plan["before_ids"].items():
        assert base.container(n)["Id"] == expected
    base.idle()
    backup = OUT / "private-before-switch"
    backup.mkdir(exist_ok=False)
    backup.chmod(0o700)
    scheduler = base.run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"]
    ).strip()
    try:
        base.run(["systemctl", "--user", "stop", "scat-auto-skill-learning.service"])
        base.idle()
        base.run(
            base.compose_command(folder / "rollback-compose.json")
            + ["stop", "--timeout", "30", "platform"]
        )
        base.idle()
        base.run(
            base.compose_command(folder / "rollback-compose.json")
            + ["stop", "--timeout", "30", "actions", "worker"]
        )
        for relative in ["var/deploy/h7b/shared/state", "var/deploy/h7b/shared/skills"]:
            for path in (ROOT / relative).glob("*.sqlite"):
                dest = backup / path.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with (
                    sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as src,
                    sqlite3.connect(dest) as dst,
                ):
                    src.backup(dst)
                    assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                dest.chmod(0o600)
        base.run(
            base.compose_command(folder / "compose.yaml")
            + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *SERVICES],
            timeout=240,
        )
        for n in SERVICES:
            assert base.container(n)["Image"] == plan["image"]
            assert base.container(n)["State"]["Health"]["Status"] == "healthy"
        assert all(
            base.container(n)["Id"] == old
            for n, old in plan["before_ids"].items()
            if n not in SERVICES
        )
        assert sha(Path("/proc/sys/kernel/random/boot_id")) == plan["boot_sha256"]
        assert sha(ROOT / "var/gpu/production.json") == plan["gpu_registration_sha256"]
        (DEPLOY / "active-release").write_text(plan["release"])
        save(
            OUT / "deployment-result.json",
            {
                "status": "applied",
                "release": plan["release"],
                "server_rebooted": False,
                "unchanged_services": [n for n in plan["before_ids"] if n not in SERVICES],
            },
        )
    except Exception:
        base.run(
            base.compose_command(folder / "rollback-compose.json")
            + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *SERVICES],
            timeout=240,
        )
        (DEPLOY / "active-release").write_text(plan["previous"])
        raise
    finally:
        if scheduler == "active":
            base.run(["systemctl", "--user", "start", "scat-auto-skill-learning.service"])
    print(json.dumps({"status": "applied", "release": plan["release"]}))


if __name__ == "__main__":
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "apply"])
    args = parser.parse_args()
    stage() if args.action == "build" else apply()
