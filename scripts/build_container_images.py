"""Offline image build from locked package artifacts; never mount host Conda environments."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "var/deploy/h7b/context"
LOGS = ROOT / "var/validation/20261027-h7b"
BASE = "docker.1ms.run/library/ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise RuntimeError("existing APP Conda required")
    lock = json.loads((CONTEXT / "artifacts.lock.json").read_text())
    for name, environment in lock["environments"].items():
        for row in [*environment["conda"], *environment["pip"].values()]:
            file = (
                CONTEXT
                / name
                / ("conda" if "name" in row else "wheels")
                / row.get("name", row.get("file"))
            )
            if hashlib.sha256(file.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("locked_artifact_changed")
    subprocess.run(
        [sys.executable, "-B", "scripts/prepare_container_context.py"], cwd=ROOT, check=True
    )
    images = {}
    for role, environment in [
        ("business", "scat-agent"),
        ("native", "scat-hermes"),
        ("author", None),
    ]:
        if shutil.disk_usage("/").free < 10 * 1024**3:
            raise RuntimeError("image_build_disk_reserve_below_10GiB")
        tag = "scat-hermes-h7b-" + role + ":20261027"
        command = [
            "docker",
            "build",
            "--network=none",
            "--pull=false",
            "--progress=plain",
            "-t",
            tag,
        ]
        if environment:
            command += [
                "--build-arg",
                "BASE_IMAGE=" + BASE,
                "--build-arg",
                "ENV_NAME=" + environment,
                "-f",
                str(CONTEXT / "Dockerfile.cpu"),
            ]
        else:
            command += [
                "--build-arg",
                "BUSINESS_IMAGE=scat-hermes-h7b-business:20261027",
                "-f",
                str(CONTEXT / "Dockerfile.author"),
            ]
        command += [str(CONTEXT)]
        with (LOGS / ("build-" + role + ".log")).open("a") as log:
            subprocess.run(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=3600
            )
        output = subprocess.run(
            ["docker", "image", "inspect", tag], capture_output=True, text=True, check=True
        )
        image = json.loads(output.stdout)[0]
        images[role] = {
            "tag": tag,
            "image_id": image["Id"],
            "bytes": image["Size"],
            "environment": environment or "scat-agent+scat-hermes",
        }
        (ROOT / "var/deploy/h7b/images-progress.json").write_text(json.dumps(images, indent=2))
        print(json.dumps(images[role]), flush=True)
    (ROOT / "var/deploy/h7b/images.lock.json").write_text(json.dumps(images, indent=2) + "\n")
    (ROOT / "var/deploy/h7b/deploy.env").write_text(
        "\n".join("SCAT_" + role.upper() + "_IMAGE=" + row["tag"] for role, row in images.items())
        + "\n"
    )
    subprocess.run(
        [sys.executable, "-B", "scripts/freeze_container_release.py"], cwd=ROOT, check=True
    )


if __name__ == "__main__":
    main()
