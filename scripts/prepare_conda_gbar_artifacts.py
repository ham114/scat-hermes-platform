"""Freeze cached Conda closure and old-image package/application artifacts; no host installs."""

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


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    os.umask(0o077)
    assert shutil.disk_usage(ROOT).free > 40 * 1024**3
    assert not (CONTEXT / "conda.lock.json").exists(), (
        "artifacts_already_frozen_review_before_retry"
    )
    CONTEXT.mkdir(parents=True, exist_ok=True)
    conda = CONTEXT / "conda"
    conda.mkdir(exist_ok=True)
    solve = json.loads((OUT / "conda-python-match-dryrun.json").read_text())
    assert solve["success"] is True and solve["dry_run"] is True
    assert not Path(solve["prefix"]).exists(), "host_environment_must_not_be_created"
    selected = {r["name"]: r for r in solve["actions"]["FETCH"]}
    assert set(selected) == {r["name"] for r in solve["actions"]["LINK"]}
    assert selected["python"]["version"] == "3.11.17", "preserve_old_image_python_patch"
    assert all(
        r["url"].startswith("https://conda.anaconda.org/conda-forge/") for r in selected.values()
    )
    (CONTEXT / "explicit-remote.txt").write_text(
        "@EXPLICIT\n" + "\n".join(r["url"] + "#" + r["sha256"] for r in selected.values()) + "\n"
    )
    needed = []
    cache = {p.name: p for p in (ROOT / "var/deploy/h7b/context").glob("*/conda/*") if p.is_file()}
    for name, row in sorted(selected.items()):
        filename = row.get("fn") or row["url"].rsplit("/", 1)[1]
        source = Path("/home/h3c/anaconda3/pkgs") / filename
        if not source.is_file():
            source = cache.get(filename, conda / filename)
        if not source.is_file():
            needed.append(
                {"name": name, "file": filename, "sha256": row["sha256"], "source": row["url"]}
            )
    (OUT / "gpu-conda-needed.json").write_text(json.dumps(needed, indent=2) + "\n")
    assert not needed, "missing_cached_artifacts_see_gpu_conda_needed"
    locked = []
    for name, row in sorted(selected.items()):
        filename = row.get("fn") or row["url"].rsplit("/", 1)[1]
        source = Path("/home/h3c/anaconda3/pkgs") / filename
        if not source.is_file():
            source = cache.get(filename, conda / filename)
        assert source.is_file(), "cached_conda_artifact_missing:" + filename
        checksum = row.get("sha256")
        assert checksum and sha(source) == checksum, filename
        dest = conda / filename
        shutil.copyfile(source, dest)
        locked.append(
            {
                "name": name,
                "version": row["version"],
                "build": row["build"],
                "file": filename,
                "sha256": checksum,
                "bytes": dest.stat().st_size,
                "source": row["url"],
            }
        )
    shutil.copyfile(ROOT / "var/deploy/h7b/context/conda-bootstrap", CONTEXT / "conda-bootstrap")
    (CONTEXT / "conda-bootstrap").chmod(0o755)
    (CONTEXT / "explicit-local.txt").write_text(
        "@EXPLICIT\n"
        + "\n".join("file:///build/conda/" + r["file"] + "#" + r["sha256"] for r in locked)
        + "\n"
    )
    shutil.copyfile(ROOT / "scripts/repack_installed_gpu_wheels.py", CONTEXT / "repack.py")
    (CONTEXT / "tmp").mkdir()
    (CONTEXT / "conda.lock.json").write_text(
        json.dumps(
            {
                "source_environment": "frozen official Conda-forge dry-run, exact source-image Python 3.11.17",
                "solver_plan_sha256": sha(OUT / "conda-python-match-dryrun.json"),
                "bootstrap_sha256": sha(CONTEXT / "conda-bootstrap"),
                "packages": locked,
                "host_installs": 0,
                "host_environments_created": 0,
            },
            indent=2,
        )
        + "\n"
    )
    for relative in [
        "gbar_service",
        "schemas",
        "release.json",
        "artifact_policy.json",
        "source_manifest.json",
        "LICENSE",
        "bentofile.yaml",
    ]:
        subprocess.run(
            [
                "docker",
                "cp",
                "scat-gbar-production-gbar-1:/opt/bento/src/" + relative,
                str(CONTEXT / relative),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=40,
        )
    artifact = ROOT / "var/deploy/gpu/conda-migration/artifacts"
    artifact.mkdir(exist_ok=False)
    subprocess.run(
        ["docker", "cp", "scat-gbar-production-gbar-1:/opt/bento/src/artifacts/.", str(artifact)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    files = {
        p.relative_to(artifact).as_posix(): {"sha256": sha(p), "bytes": p.stat().st_size}
        for p in artifact.rglob("*")
        if p.is_file()
    }
    (CONTEXT / "artifacts.lock.json").write_text(json.dumps(files, indent=2) + "\n")
    with (OUT / "gpu-wheel-reconstruction.log").open("w") as log:
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
    wheels = json.loads((CONTEXT / "wheels.lock.json").read_text())
    report = {
        "status": "artifacts_frozen_only",
        "conda_packages": len(locked),
        "conda_python": selected["python"]["version"],
        "conda_bootstrap_pip": selected.get("pip", {}).get("version"),
        "old_image_python": wheels["python"],
        "reconstructed_wheels": len(wheels["wheels"]),
        "weight_and_source_files": len(files),
        "host_environments_created": 0,
        "production_gpu_switched": False,
        "new_model_calls": 0,
    }
    (OUT / "gpu-conda-artifacts.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
