"""Offline Conda image candidate from hash-frozen packages; leaves live GPU untouched."""

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "var/deploy/gpu/conda-migration/context"
OUT = ROOT / "var/validation/20261108-release-readiness"
BASE = "docker.1ms.run/library/ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac"


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    os.umask(0o077)
    assert not (OUT / "gpu-conda-image.json").exists()
    assert shutil.disk_usage(ROOT).free > 35 * 1024**3
    conda = json.loads((CONTEXT / "conda.lock.json").read_text())
    wheels = json.loads((CONTEXT / "wheels.lock.json").read_text())
    assert len(wheels["wheels"]) == 96
    assert sha(CONTEXT / "conda-bootstrap") == conda["bootstrap_sha256"]
    for subdir, rows in [("conda", conda["packages"]), ("wheels", wheels["wheels"])]:
        for row in rows:
            assert sha(CONTEXT / subdir / row["file"]) == row["sha256"], row["file"]
    install = []
    aliases = []
    for row in wheels["wheels"]:
        filename = row["file"]
        if filename.endswith("-py2-none-any.whl"):
            with zipfile.ZipFile(CONTEXT / "wheels" / filename) as archive:
                metadata = archive.read(
                    next(n for n in archive.namelist() if n.endswith(".dist-info/WHEEL"))
                ).decode()
            assert "Tag: py3-none-any" in metadata
            alias = filename.replace("-py2-none-any.whl", "-py2.py3-none-any.whl")
            target = CONTEXT / "wheels" / alias
            if not target.exists():
                os.link(CONTEXT / "wheels" / filename, target)
            assert sha(target) == row["sha256"]
            aliases.append(
                {
                    "original": filename,
                    "install_alias": alias,
                    "sha256": row["sha256"],
                    "reason": "preserve both declared Python compatibility tags",
                }
            )
            filename = alias
        install.append("/build/wheels/" + filename + " --hash=sha256:" + row["sha256"])
    (CONTEXT / "pip-install-hashed.txt").write_text("\n".join(install) + "\n")
    (CONTEXT / "wheel-install-aliases.json").write_text(json.dumps(aliases, indent=2) + "\n")
    dockerfile = """FROM BASE AS environment
ENV TMPDIR=/build/tmp CONDA_PKGS_DIRS=/build/pkgs CONDA_NO_PLUGINS=true CONDA_SOLVER=classic
COPY conda-bootstrap /build/conda-bootstrap
COPY conda/ /build/conda/
COPY explicit-local.txt /build/explicit-local.txt
COPY wheels/ /build/wheels/
COPY pip-install-hashed.txt /build/pip-hashed.txt
RUN mkdir -p /build/tmp /build/pkgs && chmod 755 /build/conda-bootstrap && /build/conda-bootstrap create --yes --offline --no-default-packages --prefix /opt/conda/envs/scat-gbar --file /build/explicit-local.txt && /opt/conda/envs/scat-gbar/bin/python -B -m pip install --no-index --no-deps --require-hashes --find-links=/build/wheels -r /build/pip-hashed.txt && /opt/conda/envs/scat-gbar/bin/python -B -m pip check
FROM BASE
COPY --from=environment /opt/conda/envs/scat-gbar/ /opt/conda/envs/scat-gbar/
ENV PATH=/opt/conda/envs/scat-gbar/bin:$PATH PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 BENTOML_DO_NOT_TRACK=true HOME=/tmp/bento
WORKDIR /opt/bento/src
COPY --chown=1000:1000 gbar_service/ ./gbar_service/
COPY --chown=1000:1000 schemas/ ./schemas/
COPY --chown=1000:1000 release.json source_manifest.json artifact_policy.json LICENSE ./
USER 1000:1000
ENTRYPOINT ["/opt/conda/envs/scat-gbar/bin/python", "-B", "-m", "bentoml", "serve", "gbar_service.service:GBARV23Service", "--host", "0.0.0.0", "--port", "3000"]
""".replace("BASE", BASE)
    (CONTEXT / "Dockerfile").write_text(dockerfile)
    (CONTEXT / ".dockerignore").write_text("tmp\nrepack*.py\nwheels-progress.json\n")
    digest = hashlib.sha256(
        (dockerfile + sha(CONTEXT / "conda.lock.json") + sha(CONTEXT / "wheels.lock.json")).encode()
    ).hexdigest()
    tag = "scat-gbar-conda:readiness-" + digest[:12]
    attempt = len(list(OUT.glob("gpu-conda-build-*.log"))) + 1
    with (OUT / f"gpu-conda-build-{attempt}.log").open("x") as log:
        subprocess.run(
            ["docker", "build", "--network=none", "--pull=false", "-t", tag, str(CONTEXT)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=2400,
        )
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format={{.Id}}", tag], text=True
    ).strip()
    check = "import sys,json,importlib.metadata as m;import torch,bentoml,gbar_core; print(json.dumps({'python':sys.version,'torch':torch.__version__,'bentoml':bentoml.__version__,'prefix':sys.prefix,'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"
    actual = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint=/opt/conda/envs/scat-gbar/bin/python",
                image,
                "-B",
                "-c",
                check,
            ],
            text=True,
            timeout=90,
        )
    )
    assert (
        actual["python"].startswith("3.11.17")
        and actual["torch"] == "2.14.1+cu126"
        and actual["bentoml"] == "1.4.39"
    )
    for row in wheels["wheels"]:
        assert actual["packages"].get(row["name"]) == row["version"], row["name"]
    result = {
        "status": "candidate_built_imports_passed",
        "image": image,
        "tag": tag,
        "conda_packages": len(conda["packages"]),
        "preserved_distributions": len(wheels["wheels"]),
        "runtime": actual,
        "host_environments_created": 0,
        "new_model_calls": 0,
        "production_gpu_switched": False,
    }
    (OUT / "gpu-conda-image.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "runtime"}))


if __name__ == "__main__":
    main()
