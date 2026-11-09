"""Download and hash exact existing Conda/pip artifacts; never install on the host."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/deploy/h7b/context"


def sha(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("use_existing_scat_agent")
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / ".tmp/h7b"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = os.environ | {
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
    }
    bootstrap = OUT / "conda-bootstrap"
    if not bootstrap.exists():
        shutil.copyfile("/home/h3c/anaconda3/_conda", bootstrap)
    bootstrap.chmod(0o755)
    manifest = {
        "bootstrap_sha256": sha(bootstrap),
        "host_installs": 0,
        "host_new_environments": 0,
        "environments": {},
    }
    for name in ["scat-agent", "scat-hermes"]:
        inventory = json.loads(
            (ROOT / "environments/backend-h7a" / name / "inventory.json").read_text()
        )
        directory = OUT / name
        conda = directory / "conda"
        wheels = directory / "wheels"
        conda.mkdir(parents=True, exist_ok=True)
        wheels.mkdir(exist_ok=True)

        def fetch(item, conda=conda, name=name):
            filename = item["url"].rsplit("/", 1)[-1]
            destination = conda / filename
            checksum = item["checksum"]
            algorithm = "sha256" if len(checksum) == 64 else "md5"
            if not destination.exists():
                cached = Path("/home/h3c/anaconda3/pkgs") / filename
                if cached.is_file() and sha(cached, algorithm) == checksum:
                    shutil.copyfile(cached, destination)
                else:
                    staging = temporary / (name + "-" + filename + ".part")
                    with (
                        urllib.request.urlopen(item["url"], timeout=90) as response,
                        staging.open("wb") as stream,
                    ):
                        shutil.copyfileobj(response, stream)
                    os.replace(staging, destination)
            if sha(destination, algorithm) != checksum:
                raise ValueError("conda_artifact_hash_mismatch:" + filename)
            return {
                "name": filename,
                "sha256": sha(destination),
                "bytes": destination.stat().st_size,
                "source": item["url"],
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            artifacts = list(pool.map(fetch, inventory["conda"]))
        explicit = (
            "@EXPLICIT\n"
            + "\n".join(
                "file:///build/" + name + "/conda/" + row["name"] + "#" + row["sha256"]
                for row in artifacts
            )
            + "\n"
        )
        (directory / "explicit-local.txt").write_text(explicit)
        pins = ROOT / "environments/backend-h7a" / name / "pip-requirements.txt"
        excluded = {"agent-service-toolkit==0.1.0"}
        filtered = directory / "pip-download.txt"
        filtered.write_text(
            "\n".join(
                line for line in pins.read_text().splitlines() if line.strip() not in excluded
            )
            + "\n"
        )
        packages = [
            line for line in filtered.read_text().splitlines() if line and not line.startswith("#")
        ]
        logs = ROOT / "var/validation/20261027-h7b/downloads" / name
        logs.mkdir(parents=True, exist_ok=True)

        def fetch_wheel(package, name=name, wheels=wheels, logs=logs):
            from packaging.utils import canonicalize_name, parse_wheel_filename

            package_name, _, version = package.partition("==")
            if any(
                canonicalize_name(parse_wheel_filename(path.name)[0])
                == canonicalize_name(package_name)
                and str(parse_wheel_filename(path.name)[1]) == version
                for path in wheels.glob("*.whl")
            ):
                return
            command = [
                str(Path("/home/h3c/anaconda3/envs") / name / "bin/python"),
                "-B",
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--only-binary=:all:",
                "--index-url",
                "https://pypi.tuna.tsinghua.edu.cn/simple",
                "--dest",
                str(wheels),
                "--timeout",
                "45",
                "--retries",
                "3",
                package,
            ]
            with (logs / (package.split("==")[0] + ".log")).open("a") as log:
                for attempt in range(3):
                    result = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=240,
                        check=False,
                    )
                    if not result.returncode:
                        return
                raise ValueError("wheel_download_failed:" + package)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(fetch_wheel, packages))
        from packaging.utils import canonicalize_name, parse_wheel_filename

        wheel_rows = {}
        for path in sorted(wheels.glob("*.whl")):
            package, version, _, _ = parse_wheel_filename(path.name)
            identity = canonicalize_name(package) + "==" + str(version)
            wheel_rows[identity] = {
                "file": path.name,
                "sha256": sha(path),
                "bytes": path.stat().st_size,
            }
        expected = {
            canonicalize_name(line.strip().split("==")[0]) + "==" + line.strip().split("==")[1]
            for line in pins.read_text().splitlines()
            if line.strip() and not line.startswith("#") and line.strip() not in excluded
        }
        if set(wheel_rows) != expected:
            raise ValueError("downloaded_wheels_differ_from_inventory:" + name)
        (directory / "pip-hashed.txt").write_text(
            "\n".join(
                pin + " --hash=sha256:" + wheel_rows[pin]["sha256"] for pin in sorted(expected)
            )
            + "\n"
        )
        manifest["environments"][name] = {
            "conda": artifacts,
            "pip": wheel_rows,
            "inventory_sha256": sha(ROOT / "environments/backend-h7a" / name / "inventory.json"),
            "local_sources": inventory["local"],
            "pip_source": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "excluded_project_distributions": sorted(excluded & set(pins.read_text().splitlines())),
        }
        (OUT / "artifacts-progress.json").write_text(json.dumps(manifest, indent=2))
        print(
            json.dumps(
                {"environment": name, "conda_artifacts": len(artifacts), "wheels": len(wheel_rows)}
            ),
            flush=True,
        )
    (OUT / "artifacts.lock.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("all_existing_environment_artifacts_locked", flush=True)


if __name__ == "__main__":
    main()
