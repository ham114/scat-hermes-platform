"""Source-only derivative Conda images and reversible activation of owned services."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/autonomy"
LOGS = ROOT / "var/validation/20261028-autonomy"
APP = "/home/h3c/anaconda3/envs/scat-agent/bin/python"


def run(*args, **kwargs):
    return subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=kwargs.pop("timeout", 180),
        **kwargs,
    ).stdout


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("unsafe_existing_private_file")
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)


def prepare():
    DEPLOY.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for key in ("memory", "author", "trial", "identity", "native-author"):
        private(DEPLOY / "secrets" / (key + ".key"), secrets.token_urlsafe(48))
    private(DEPLOY / "config.json", (ROOT / "config/autonomy.example.json").read_text())
    private(
        DEPLOY / "proxy.json",
        json.dumps(
            {
                "daily_requests": 240,
                "daily_units": 8000000,
                "lifetime_requests": 240,
                "lifetime_units": 8000000,
            }
        ),
    )
    sys.path.insert(0, str(ROOT / "src"))
    from hermes_runtime.autonomy_config import load_autonomy
    from hermes_runtime.experience_learning import LearningLedger

    LearningLedger(ROOT, load_autonomy(ROOT, "var/deploy/autonomy/config.json").learning)
    run(
        "/home/h3c/anaconda3/envs/scat-hermes/bin/python",
        "-B",
        "scripts/profile_manage.py",
        "init",
        "--root",
        "var/autonomy/profiles",
    )


def build(*, skill_scripts=False):
    import prepare_container_context as context

    context.OUT = DEPLOY / "context"
    context.OUT.mkdir(parents=True, exist_ok=True)
    context.main()
    manifest = json.loads((context.OUT / "source.lock.json").read_text())
    # Trusted trials need their frozen holdout suite inside author-api as well.
    for path in sorted((ROOT / "evaluations").glob("*.json")):
        relative = path.relative_to(ROOT)
        target = context.OUT / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True, indent=2)
    release = ("20261028-s7b-" if skill_scripts else "20261028-") + hashlib.sha256(
        encoded.encode()
    ).hexdigest()[:12]
    archive = DEPLOY / "releases" / release
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "source.lock.json").write_text(encoded)
    previous = json.loads((ROOT / "var/deploy/h7b/images.lock.json").read_text())
    images = {}
    for role, row in previous.items():
        if (
            run("docker", "image", "inspect", row["tag"], "--format", "{{.Id}}").strip()
            != row["image_id"]
        ):
            raise ValueError("base_image_identity_changed")
        tag = "scat-hermes-autonomy-" + role + ":" + release
        dockerfile = context.OUT / ("Dockerfile." + role)
        dockerfile.write_text(
            "FROM " + row["tag"] + "\nCOPY --chown=1000:1000 app/ " + str(ROOT) + "/\n"
        )
        if shutil.disk_usage(ROOT).free < 8 * 1024**3:
            raise ValueError("image_disk_reserve_below_8GiB")
        with (LOGS / ("build-" + role + ".log")).open("a") as log:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--network=none",
                    "--pull=false",
                    "--progress=plain",
                    "-f",
                    str(dockerfile),
                    "-t",
                    tag,
                    str(context.OUT),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=900,
            )
        images[role] = {
            "tag": tag,
            "image_id": run("docker", "image", "inspect", tag, "--format", "{{.Id}}").strip(),
            "base_image_id": row["image_id"],
        }
    compose = json.loads((ROOT / "infra/compose/compose.yaml").read_text())
    for service in compose["services"].values():
        role = (
            "author"
            if service["command"] == ["author-api"]
            else "native"
            if service["command"][0] in {"main", "author", "job-control"}
            else "business"
        )
        service["image"] = images[role]["image_id"]
    platform = compose["services"]["platform"]
    platform["environment"]["SCAT_AUTONOMY_CONFIG"] = "var/deploy/autonomy/config.json"

    def mount(service, relative, readonly):
        path = str(ROOT / relative)
        service["volumes"].append(
            {
                "type": "bind",
                "source": path,
                "target": path,
                "read_only": readonly,
                "bind": {"create_host_path": False},
            }
        )

    for path, readonly in [
        ("var/deploy/autonomy/config.json", True),
        ("var/deploy/autonomy/secrets/identity.key", True),
        ("var/deploy/honcho-production/secrets/auth-signing.key", True),
        ("var/autonomy/memory", False),
        ("var/autonomy/learning", True),
    ]:
        mount(platform, path, readonly)
    author_api = compose["services"]["author-api"]
    author_api["environment"]["SCAT_BOUNDED_MODEL_ROLE"] = "trial"
    mount(author_api, "var/deploy/autonomy/secrets/trial.key", True)
    if skill_scripts:
        for service in compose["services"].values():
            if service["command"][0] in {"main", "job-control"}:
                service["environment"]["SCAT_SKILL_SCRIPTS_ENABLED"] = "1"
    (archive / "features.json").write_text(
        json.dumps({"skill_scripts": skill_scripts, "automatic_script_authoring": False}) + "\n"
    )
    (ROOT / "var/autonomy/memory").mkdir(parents=True, exist_ok=True)
    for name, value in [("images.lock.json", images), ("compose.yaml", compose)]:
        (archive / name).write_text(json.dumps(value, indent=2) + "\n")
    with tarfile.open(archive / "source.tar.gz", "w:gz") as stream:
        stream.add(context.OUT / "app", arcname="app")
    (DEPLOY / "current-release").write_text(release)
    print(json.dumps({"release": release, "images": images}), flush=True)


def activate():
    folder = DEPLOY / "releases" / (DEPLOY / "current-release").read_text().strip()
    for row in json.loads((folder / "images.lock.json").read_text()).values():
        if (
            run("docker", "image", "inspect", row["tag"], "--format", "{{.Id}}").strip()
            != row["image_id"]
        ):
            raise ValueError("deployment_image_identity_changed")
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
        with sqlite3.connect(ROOT / path) as db:
            if db.execute(query).fetchone()[0]:
                raise ValueError("active_or_unresolved_work_prevents_restart")
    run(
        "docker",
        "compose",
        "--project-name",
        "scat-hermes-h7b",
        "-f",
        str(folder / "compose.yaml"),
        "--profile",
        "author",
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "180",
        timeout=240,
    )
    units = DEPLOY / "units"
    units.mkdir(exist_ok=True)
    path = units / "scat-auto-skill-learning.service"
    path.write_text(
        "[Unit]\nDescription=SCAT verified experience skill author scheduler\nAfter=network-online.target\n[Service]\nType=simple\nWorkingDirectory="
        + str(ROOT)
        + "\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=SCAT_AUTONOMY_CONFIG=var/deploy/autonomy/config.json\nExecStart="
        + APP
        + " -B scripts/run_auto_skill_learning.py\nRestart=on-failure\nRestartSec=30\nUMask=0077\nStandardOutput=append:"
        + str(LOGS / "scheduler.log")
        + "\nStandardError=inherit\n[Install]\nWantedBy=default.target\n"
    )
    run("systemctl", "--user", "link", str(path))
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", path.name)
    (DEPLOY / "active-release").write_text(folder.name)
    print(
        json.dumps(
            {
                "active_release": folder.name,
                "platform": "http://127.0.0.1:18120",
                "automatic_publication": False,
            }
        )
    )


def main():
    if sys.executable != APP:
        raise RuntimeError("existing_APP_Anaconda_required")
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "build", "activate"])
    parser.add_argument("--skill-scripts", action="store_true", help="Enable S7-B in main/workers")
    args = parser.parse_args()
    if args.skill_scripts and args.action != "build":
        parser.error("--skill-scripts is valid only for build")
    if args.action == "build":
        build(skill_scripts=args.skill_scripts)
    else:
        {"prepare": prepare, "activate": activate}[args.action]()


if __name__ == "__main__":
    main()
