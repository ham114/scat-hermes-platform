"""Enable reviewed GBAR on the current release; preserve old services and rollback."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import deploy_forecast_contract as deploy

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
DEPLOY = ROOT / "var/deploy/autonomy"
FILES = [
    "src/workflow_runtime/catalog.py",
    "scripts/run_hermes_api.py",
    "src/analysis_runs/artifact_repository.py",
    "src/workflow_runtime/gpu.py",
]


def configure_gpu(service, root):
    """Config is immutable in containers; SQLite and its journals share one host directory."""
    service["environment"]["SCAT_GPU_CONFIG_FILE"] = "var/gpu/production.json"
    service["volumes"] = [
        v for v in service["volumes"] if not v["target"].startswith(str(root / "var/gpu"))
    ] + [
        {
            "type": "bind",
            "source": str(root / "var/gpu"),
            "target": str(root / "var/gpu"),
            "read_only": False,
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": str(root / "var/gpu/production.json"),
            "target": str(root / "var/gpu/production.json"),
            "read_only": True,
            "bind": {"create_host_path": False},
        },
    ]


def stage():
    previous = (DEPLOY / "active-release").read_text().strip()
    original = DEPLOY / "releases" / previous
    compose = json.loads((original / "compose.yaml").read_text())
    live = {name: deploy.container(name) for name in deploy.SERVICES}
    for name, row in live.items():
        assert row["Image"] == compose["services"][name]["image"]
        assert row["State"]["Health"]["Status"] == "healthy"
        compose["services"][name]["dns"] = row["HostConfig"].get("Dns") or []
    skill = deploy.candidate_skill()
    skill["manifest"]["bundle_version"] = "s5-gbar-production.1.0"
    for entry in skill["manifest"]["skills"]:
        if entry["name"] == "scat-experiment":
            entry["version"] = "0.2.0"
            payload = (ROOT / "skills/scat-experiment/SKILL.md").read_bytes()
            skill["files"]["scat-experiment/SKILL.md"] = payload.decode()
            entry["files"]["SKILL.md"] = hashlib.sha256(payload).hexdigest()
    skill["target_sha"] = hashlib.sha256(
        json.dumps(
            skill["manifest"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    payloads = {p: (ROOT / p).read_text() for p in FILES}
    payloads["scripts/run_hermes_api.py"] = deploy.replace_function(
        deploy.read_source("hermes-main", "scripts/run_hermes_api.py"),
        payloads["scripts/run_hermes_api.py"],
        "profile_config",
    )
    hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in payloads.items()}
    registration_sha = hashlib.sha256((ROOT / "var/gpu/production.json").read_bytes()).hexdigest()
    identity = hashlib.sha256(
        json.dumps(
            {
                "previous": previous,
                "files": hashes,
                "skill": skill["target_sha"],
                "gpu": registration_sha,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]
    release = "20261108-gbar-production-" + identity
    folder = DEPLOY / "releases" / release
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    (folder / "rollback-compose.json").write_text(json.dumps(compose, indent=2))
    (folder / "skill-release.json").write_text(json.dumps(skill, ensure_ascii=False, indent=2))
    for p, value in payloads.items():
        path = folder / "patch" / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    bases = {"business": live["platform"]["Image"], "native": live["hermes-main"]["Image"]}
    images = json.loads((original / "images.lock.json").read_text())
    for role, base in bases.items():
        assert all(
            live[n]["Image"] == base
            for n in (
                ["platform", "actions", "worker"]
                if role == "business"
                else ["hermes-main", "hermes-worker"]
            )
        )
        assert shutil.disk_usage(ROOT).free >= 5 * 1024**3
        tag = "scat-hermes-gbar-base-" + role + ":" + identity
        deploy.run(["docker", "image", "tag", base, tag])
        dockerfile = folder / ("Dockerfile." + role)
        dockerfile.write_text(
            "FROM "
            + tag
            + "\n"
            + "".join(
                "COPY --chown=1000:1000 patch/" + p + " " + str(ROOT / p) + "\n" for p in payloads
            )
        )
        target = "scat-hermes-gbar-production-" + role + ":" + identity
        output = deploy.run(
            [
                "docker",
                "build",
                "--network=none",
                "--pull=false",
                "-f",
                str(dockerfile),
                "-t",
                target,
                str(folder),
            ],
            timeout=300,
        )
        (OUT / ("build-" + role + ".log")).write_text(output)
        images[role] = {
            "tag": target,
            "image_id": deploy.run(
                ["docker", "image", "inspect", target, "--format", "{{.Id}}"]
            ).strip(),
            "base_image_id": base,
        }
    for name in deploy.SERVICES:
        compose["services"][name]["image"] = images[
            "native" if name.startswith("hermes-") else "business"
        ]["image_id"]
        configure_gpu(compose["services"][name], ROOT)
    (folder / "compose.yaml").write_text(json.dumps(compose, indent=2))
    (folder / "images.lock.json").write_text(json.dumps(images, indent=2))
    source = json.loads((original / "source.lock.json").read_text())
    source.update(hashes)
    (folder / "source.lock.json").write_text(json.dumps(source, indent=2))
    if (original / "features.json").exists():
        shutil.copy2(original / "features.json", folder / "features.json")
    plan = {
        "previous": previous,
        "release": release,
        "services": deploy.SERVICES,
        "source_hashes": hashes,
        "base_images": bases,
        "images": images,
        "skill_change": {
            k: skill[k] for k in ["previous_sha", "previous_generation", "target_sha"]
        },
        "gpu_configuration_changed": True,
        "gpu_registration_file_sha256": registration_sha,
        "author_services_changed": False,
        "applied": False,
        "boot_sha256": hashlib.sha256(
            Path("/proc/sys/kernel/random/boot_id").read_bytes()
        ).hexdigest(),
    }
    (OUT / "deployment-plan.json").write_text(json.dumps(plan, indent=2))
    deploy.run(deploy.compose_command(folder / "compose.yaml") + ["config", "--quiet"])
    print(json.dumps({"release": release, "built": True, "applied": False}))


if __name__ == "__main__":
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    deploy.OUT = OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "apply"])
    args = parser.parse_args()
    if args.action == "build":
        stage()
    else:
        plan = json.loads((OUT / "deployment-plan.json").read_text())
        assert (
            hashlib.sha256((ROOT / "var/gpu/production.json").read_bytes()).hexdigest()
            == plan["gpu_registration_file_sha256"]
        )
        with sqlite3.connect(
            "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
        ) as source:
            assert not source.execute(
                "SELECT 1 FROM gpu_requests WHERE state IN ('running','unknown')"
            ).fetchone()
            backup = OUT / ("gpu-receipts-before-switch-" + plan["release"] + ".sqlite")
            assert not backup.exists()
            with sqlite3.connect(backup) as target:
                source.backup(target)
            backup.chmod(0o600)
        deploy.apply()
