"""Resolve fixed-source Honcho dependencies for a candidate Conda image only."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/deploy/honcho"
LOG = ROOT / "var/validation/20261027-honcho"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    os.umask(0o077)
    source = OUT / "source"
    context = OUT / "image-context"
    wheels = context / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    temp = ROOT / ".tmp/honcho-test"
    temp.mkdir(parents=True, exist_ok=True)
    dependencies = tomllib.loads((source / "pyproject.toml").read_text())["project"]["dependencies"]
    sdk = tomllib.loads((source / "sdks/python/pyproject.toml").read_text())["project"]["version"]
    (context / "requirements.in").write_text(
        "\n".join([*dependencies, "honcho-ai==" + sdk, "numpy>=2.3,<3"]) + "\n"
    )
    native = "/home/h3c/anaconda3/envs/scat-hermes/bin/python"
    command = [
        native,
        "-B",
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--index-url",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--find-links",
        str(ROOT / "var/deploy/h7b/context/scat-hermes/wheels"),
        "--find-links",
        str(ROOT / "var/deploy/h7b/context/scat-agent/wheels"),
        "--constraint",
        str(ROOT / "environments/backend-h7a/scat-hermes/pip-requirements.txt"),
        "--dest",
        str(wheels),
        "--timeout",
        "45",
        "--retries",
        "2",
        "-r",
        str(context / "requirements.in"),
    ]
    with (LOG / "dependency-download.log").open("a") as stream:
        subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ
            | {"TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp), "PIP_NO_CACHE_DIR": "1"},
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1500,
        )
    from packaging.utils import parse_wheel_filename

    lock = []
    pins = []
    for path in sorted(wheels.glob("*.whl")):
        name, version, _, _ = parse_wheel_filename(path.name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        pins.append(str(name) + "==" + str(version) + " --hash=sha256:" + digest)
        lock.append({"file": path.name, "sha256": digest, "bytes": path.stat().st_size})
    (context / "requirements.lock").write_text("\n".join(pins) + "\n")
    (OUT / "dependency-artifacts.json").write_text(json.dumps(lock, indent=2) + "\n")
    for name in ["src", "migrations", "docker"]:
        shutil.copytree(
            source / name,
            context / "app" / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in ["alembic.ini", "pyproject.toml", "config.toml.example"]:
        shutil.copyfile(source / name, context / "app" / name)
    for name in ["hermes-plugin-honcho", "harness-plugin-core"]:
        shutil.copytree(
            source / name,
            context / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"),
        )
    base = json.loads((ROOT / "var/deploy/h7b/images.lock.json").read_text())["native"]["tag"]
    dockerfile = (
        "FROM "
        + base
        + '\nUSER root\nCOPY wheels/ /home/h3c/project/python/scat-hermes-platform/.tmp/honcho-build/wheels/\nCOPY requirements.lock /home/h3c/project/python/scat-hermes-platform/.tmp/honcho-build/requirements.lock\nRUN /opt/conda/envs/scat-hermes/bin/python -B -m pip install --no-index --require-hashes --find-links=/home/h3c/project/python/scat-hermes-platform/.tmp/honcho-build/wheels -r /home/h3c/project/python/scat-hermes-platform/.tmp/honcho-build/requirements.lock && /opt/conda/envs/scat-hermes/bin/python -B -m pip check\nCOPY app/ /home/h3c/project/python/scat-hermes-platform/var/deploy/honcho/source/\nCOPY hermes-plugin-honcho/ /home/h3c/project/python/scat-hermes-platform/var/deploy/honcho/plugin/\nCOPY harness-plugin-core/ /home/h3c/project/python/scat-hermes-platform/var/deploy/honcho/harness-plugin-core/\nENV PYTHONPATH=/home/h3c/project/python/scat-hermes-platform/var/deploy/honcho/source PYTHONUNBUFFERED=1\nWORKDIR /home/h3c/project/python/scat-hermes-platform/var/deploy/honcho/source\nUSER 1000:1000\nENTRYPOINT ["/opt/conda/envs/scat-hermes/bin/python","-B"]\n'
    )
    for source_name in ["app", "hermes-plugin-honcho", "harness-plugin-core"]:
        dockerfile = dockerfile.replace(
            "COPY " + source_name + "/", "COPY --chown=1000:1000 " + source_name + "/"
        )
    (context / "Dockerfile").write_text(dockerfile)
    if shutil.disk_usage("/").free < 10 * 1024**3:
        raise ValueError("candidate_build_disk_reserve_below_10GiB")
    tag = "scat-honcho-conda-test:20261027"
    with (LOG / "image-build.log").open("a") as stream:
        subprocess.run(
            [
                "docker",
                "build",
                "--network=none",
                "--pull=false",
                "--progress=plain",
                "-t",
                tag,
                str(context),
            ],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )
    identity = subprocess.run(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = {
        "image": tag,
        "image_id": identity,
        "wheels": len(lock),
        "host_installs": 0,
        "source": json.loads((OUT / "source.lock.json").read_text()),
    }
    (OUT / "image.lock.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
