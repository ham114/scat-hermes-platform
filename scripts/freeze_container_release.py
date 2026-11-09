"""Archive immutable image identities and source hashes for an owned Compose release."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/h7b"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    os.umask(0o077)
    source = DEPLOY / "context/source.lock.json"
    release = "20261027-" + hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    folder = DEPLOY / "releases" / release
    folder.mkdir(parents=True, exist_ok=True)
    images = json.loads((DEPLOY / "images.lock.json").read_text())
    for role, row in images.items():
        identity = subprocess.run(
            ["docker", "image", "inspect", row["tag"], "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if identity != row["image_id"]:
            raise ValueError("refuse_freezing_changed_tag")
        tag = "scat-hermes-h7b-" + role + ":" + release
        previous = subprocess.run(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if previous.returncode == 0 and previous.stdout.strip() != identity:
            raise ValueError("immutable_release_tag_already_exists")
        subprocess.run(["docker", "tag", identity, tag], check=True)
        row["tag"] = tag
    lock = json.dumps(images, indent=2) + "\n"
    existing = folder / "images.lock.json"
    if existing.exists() and existing.read_text() != lock:
        raise ValueError("release_id_already_has_different_images")
    if existing.exists():
        # Keep the operational tools/source/Compose captured at publication.
        # A later host-only audit edit must not mutate an archived release.
        (DEPLOY / "images.lock.json").write_text(lock)
        shutil.copyfile(folder / "deploy.env", DEPLOY / "deploy.env")
        print(json.dumps({"release": release, "existing_release_reused": True}))
        return
    existing.write_text(lock)
    deploy_env = (
        "\n".join("SCAT_" + role.upper() + "_IMAGE=" + row["tag"] for role, row in images.items())
        + "\n"
    )
    (folder / "deploy.env").write_text(deploy_env)
    for name in ["source.lock.json", "artifacts.lock.json"]:
        shutil.copyfile(DEPLOY / "context" / name, folder / name)
    for name in ["compose.yaml", "compose.smoke.yaml", "GPU_MIGRATION.md", "Dockerfile.cpu"]:
        shutil.copyfile(ROOT / "infra/compose" / name, folder / name)
    tools = folder / "host-tools"
    tools.mkdir(exist_ok=True)
    for path in (ROOT / "scripts").glob("*container*.py"):
        shutil.copyfile(path, tools / path.name)
    shutil.copyfile(ROOT / "scripts/generate_compose_h7b.py", tools / "generate_compose_h7b.py")
    (folder / "host-tools.lock.json").write_text(
        json.dumps(
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in tools.iterdir()},
            indent=2,
        )
        + "\n"
    )
    archive = folder / "source.tar.gz"
    if not archive.exists():
        with tarfile.open(str(archive) + ".tmp", "w:gz") as stream:
            stream.add(DEPLOY / "context/app", arcname="app")
        Path(str(archive) + ".tmp").replace(archive)
    (DEPLOY / "images.lock.json").write_text(lock)
    (DEPLOY / "deploy.env").write_text(deploy_env)
    print(
        json.dumps(
            {
                "release": release,
                "images": images,
                "source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
