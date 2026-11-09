"""Resume only missing frozen-image wheels; completed wheel hashes must remain identical."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "var/deploy/gpu/conda-migration/context"
OUT = ROOT / "var/validation/20261108-release-readiness"
IMAGE = "sha256:0bbae03be3d2c1340350d7eca6c9168b8735be57adfb61883fda8bad914d7bf4"


def main():
    os.umask(0o077)
    assert not (CONTEXT / "wheels.lock.json").exists(), "already_completed"
    assert (CONTEXT / "conda.lock.json").is_file() and (CONTEXT / "artifacts.lock.json").is_file()
    old = CONTEXT / "repack.py"
    old_sha = hashlib.sha256(old.read_bytes()).hexdigest()
    backup = CONTEXT / "repack-first.py"
    if not backup.exists():
        shutil.copyfile(old, backup)
    rows = json.loads((CONTEXT / "wheels-progress.json").read_text())
    for row in rows:
        row.setdefault("builder_sha256", old_sha)
    (CONTEXT / "wheels-progress.json").write_text(json.dumps(rows, indent=2) + "\n")
    shutil.copyfile(ROOT / "scripts/repack_installed_gpu_wheels.py", old)
    attempt = len(list(OUT.glob("gpu-wheel-reconstruction-resume-attempt-*.log"))) + 1
    logfile = OUT / f"gpu-wheel-reconstruction-resume-attempt-{attempt}.log"
    with logfile.open("x") as log:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--cpus=2",
                "--memory=3g",
                "--user",
                "1000:1000",
                "-e",
                "TMPDIR=/build/tmp",
                "--mount",
                "type=bind,src=" + str(CONTEXT) + ",dst=/build",
                "--entrypoint",
                "python",
                IMAGE,
                "-B",
                "/build/repack.py",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1800,
        )
    lock = json.loads((CONTEXT / "wheels.lock.json").read_text())
    conda = json.loads((CONTEXT / "conda.lock.json").read_text())
    report = {
        "status": "artifacts_frozen_only",
        "conda_packages": len(conda["packages"]),
        "conda_python": next(r["version"] for r in conda["packages"] if r["name"] == "python"),
        "old_image_python": lock["python"],
        "reconstructed_wheels": len(lock["wheels"]),
        "preserved_completed_wheels": len(rows),
        "host_environments_created": 0,
        "production_gpu_switched": False,
        "new_model_calls": 0,
    }
    (OUT / "gpu-conda-artifacts.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
