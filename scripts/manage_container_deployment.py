"""Operate only the owned Compose project; never controls the existing gateway/GPU services."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/h7b"
PROJECT = "scat-hermes-h7b"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise RuntimeError("existing APP Conda required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "up", "status", "down"])
    parser.add_argument("--author", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--release", help="Archived 20261027-<12 hex> release; validates its image identities"
    )
    args = parser.parse_args()
    release = DEPLOY
    compose = ROOT / "infra/compose"
    if args.release:
        if not re.fullmatch(r"20261027-[a-f0-9]{12}", args.release):
            raise ValueError("invalid_owned_release")
        release = DEPLOY / "releases" / args.release
        compose = release
    command = [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--env-file",
        str(release / "deploy.env"),
        "-f",
        str(compose / "compose.yaml"),
    ]
    if args.smoke:
        command += ["-f", str(compose / "compose.smoke.yaml")]
    if args.author:
        command += ["--profile", "author"]
    for row in json.loads((release / "images.lock.json").read_text()).values():
        actual = subprocess.run(
            ["docker", "image", "inspect", row["tag"], "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if actual != row["image_id"]:
            raise ValueError("image_tag_identity_changed")
    subprocess.run(command + ["config", "--quiet"], cwd=ROOT, check=True, timeout=30)
    if args.action == "validate":
        print(json.dumps({"status": "valid", "project": PROJECT, "gpu_profiles_enabled": False}))
        return
    if args.action == "up":
        if shutil.disk_usage("/").free < 5 * 1024**3:
            raise RuntimeError("deployment_disk_reserve_below_5GiB")
        subprocess.run(
            command + ["up", "-d", "--wait", "--wait-timeout", "180"],
            cwd=ROOT,
            check=True,
            timeout=240,
        )
    elif args.action == "down":
        subprocess.run(command + ["down", "--timeout", "40"], cwd=ROOT, check=True, timeout=120)
    ids = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project=" + PROJECT,
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    rows = []
    for identity in ids:
        value = json.loads(
            subprocess.run(
                ["docker", "inspect", identity], capture_output=True, text=True, check=True
            ).stdout
        )[0]
        rows.append(
            {
                "service": value["Config"]["Labels"]["com.docker.compose.service"],
                "id": value["Id"][:12],
                "image_id": value["Image"],
                "status": value["State"]["Status"],
                "health": value["State"].get("Health", {}).get("Status"),
                "network": value["HostConfig"]["NetworkMode"],
                "read_only_root": value["HostConfig"]["ReadonlyRootfs"],
                "user": value["Config"]["User"],
                "memory_bytes": value["HostConfig"]["Memory"],
                "nano_cpus": value["HostConfig"]["NanoCpus"],
            }
        )
    report = {
        "project": PROJECT,
        "containers": rows,
        "author_enabled": args.author,
        "synthetic_workflow_source": args.smoke,
    }
    (DEPLOY / "last-status.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
