"""Provision the approved dedicated Conda prefix; never touch scat-agent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREFIX = Path("/home/h3c/anaconda3/envs/scat-hermes")
CONDA = Path("/home/h3c/anaconda3/bin/conda")


def main() -> int:
    if ROOT.name != "scat-hermes-platform" or sys.platform != "linux":
        raise RuntimeError("Run only from the designated Linux project workspace")
    if PREFIX.parent.resolve() != Path("/home/h3c/anaconda3/envs").resolve():
        raise RuntimeError("Unapproved Conda prefix")
    lock = json.loads((ROOT / "services/hermes/upstream.lock.json").read_text())
    work = ROOT / ".tmp/hermes-setup"
    logs = ROOT / "var/setup"
    for p in [work, logs, work / "tmp", work / "cache", work / "conda-pkgs"]:
        p.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        TMPDIR=str(work / "tmp"),
        TMP=str(work / "tmp"),
        TEMP=str(work / "tmp"),
        XDG_CACHE_HOME=str(work / "cache"),
        CONDA_PKGS_DIRS=str(work / "conda-pkgs"),
        PIP_CACHE_DIR=str(work / "cache/pip"),
        PYTHONDONTWRITEBYTECODE="1",
        HERMES_HOME=str(ROOT / "var/hermes/single-user"),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    state = {
        "status": "running",
        "stage": "starting",
        "started_at": time.time(),
        "prefix": str(PREFIX),
        "commit": lock["commit"],
    }
    state_path = logs / "hermes-setup-status.json"

    def save():
        state_path.write_text(json.dumps(state, indent=2))

    def run(stage, argv, *, cwd=ROOT, timeout=900):
        state["stage"] = stage
        save()
        with (logs / "hermes-setup.log").open("a") as log:
            log.write("\nSTAGE " + stage + "\n")
            log.flush()
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        if result.returncode:
            raise RuntimeError(f"{stage} exited {result.returncode}")

    save()
    try:
        source = ROOT / lock["source_directory"]
        source.parent.mkdir(exist_ok=True)
        artifact = work / "source.tar.gz"
        manifest_path = work / "source-manifest.json"
        if not source.resolve().is_relative_to(ROOT):
            raise RuntimeError("Source path escaped workspace")
        if not source.exists() and artifact.is_file():
            state["stage"] = "verify-and-unpack-source-snapshot"
            save()
            manifest = json.loads(manifest_path.read_text())
            with artifact.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != manifest["runtime_archive_sha256"] or manifest["commit"] != lock["commit"]:
                raise RuntimeError("Source archive identity mismatch")
            staging = source.with_name(source.name + ".h1-staging")
            if staging.exists():
                raise RuntimeError("Unexpected source staging directory; inspect before retry")
            staging.mkdir()
            try:
                seen = set()
                with tarfile.open(artifact, "r:gz") as bundle:
                    for member in bundle:
                        target = (staging / member.name).resolve()
                        if not member.isfile() or not target.is_relative_to(staging):
                            raise RuntimeError("Unsafe source archive member")
                        content = bundle.extractfile(member).read()
                        actual = hashlib.sha1(
                            b"blob " + str(len(content)).encode() + b"\0" + content
                        ).hexdigest()
                        if manifest["git_blob_sha1"].get(member.name) != actual:
                            raise RuntimeError("Source file identity mismatch")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                        target.chmod(member.mode & 0o777)
                        seen.add(member.name)
                if seen != set(manifest["git_blob_sha1"]):
                    raise RuntimeError("Source snapshot is incomplete")
                (staging / ".scat-source.json").write_text(json.dumps(manifest))
                staging.rename(source)
            finally:
                if staging.exists() and staging.resolve().is_relative_to(ROOT):
                    shutil.rmtree(staging)
        elif not source.exists():
            run(
                "clone-pinned-source",
                [
                    "git",
                    "-c",
                    "http.version=HTTP/1.1",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    "--depth",
                    "1",
                    "--branch",
                    lock["tag"],
                    lock["repository"],
                    str(source),
                ],
            )
            run(
                "select-runtime-source",
                [
                    "git",
                    "sparse-checkout",
                    "set",
                    "--no-cone",
                    "/*",
                    "!/website/",
                    "!/tests/",
                    "!/docs/",
                    "!/hermes-desktop/",
                ],
                cwd=source,
            )
            run("checkout-pinned-source", ["git", "checkout", lock["commit"]], cwd=source)
        if (source / ".git").exists():
            actual = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True
            ).strip()
        else:
            actual = json.loads((source / ".scat-source.json").read_text()).get("commit")
        if actual != lock["commit"]:
            raise RuntimeError("Existing source does not match upstream lock")
        python = PREFIX / "bin/python"
        if not python.is_file():
            if PREFIX.exists():
                raise RuntimeError("Partial or unexpected Conda prefix; inspect before retry")
            run(
                "create-approved-conda-environment",
                [
                    str(CONDA),
                    "create",
                    "--yes",
                    "--prefix",
                    str(PREFIX),
                    "--override-channels",
                    "--channel",
                    "conda-forge",
                    "python=3.14",
                    "pip",
                ],
            )
        version = subprocess.check_output(
            [
                str(python),
                "-B",
                "-c",
                'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
            ],
            text=True,
        ).strip()
        if version != "3.14":
            raise RuntimeError("Existing dedicated environment is not Python 3.14")
        run(
            "install-hermes-core-mcp-api",
            [
                str(python),
                "-B",
                "-m",
                "pip",
                "install",
                "--no-input",
                "--editable",
                str(source) + "[mcp]",
                "aiohttp==3.14.3",
            ],
        )
        run("pip-check", [str(python), "-B", "-m", "pip", "check"])
        state.update(status="completed", stage="installed", finished_at=time.time())
        save()
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        state.update(
            status="failed", error_type=type(exc).__name__, error=str(exc), finished_at=time.time()
        )
        save()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
