"""Install pinned vendor CDI tools and a versioned userspace root, without daemon restart.

Run with system Python via sudo only after candidate generation succeeded. Does not
modify the host kernel module, system NVIDIA packages, or existing GPU containers.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "var/deploy/gpu/cdi-assets"
OUT = ROOT / "var/validation/20261108-release-readiness"
VERSION = "580.173.02"


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    assert os.geteuid() == 0
    assert VERSION in Path("/proc/driver/nvidia/version").read_text()
    archive = BASE / f"NVIDIA-Linux-x86_64-{VERSION}-no-compat32.run"
    deb = BASE / "nvidia-container-toolkit-base_1.19.1-1_amd64.deb"
    assert sha(archive) == "e90b79270cfc12fc9374721df4831c6cf0ec332f8ce993c40bb018b0e28bf238"
    assert sha(deb) == "b6c5b4e77a28cde0197cc0e64edf75538604775d9f8aea502cef667e7e5b2132"
    assert (OUT / "cdi-driver-root.json").is_file()
    target = Path("/opt/scat-nvidia") / VERSION
    tools = Path("/usr/local/libexec/scat-nvidia")
    spec = Path("/etc/cdi/scat-nvidia.json")
    assert not target.exists() and not spec.exists(), "existing_install_review_before_retry"
    assert not tools.exists(), "existing_tool_directory_review"
    target.mkdir(parents=True, mode=0o755)
    tools.mkdir(parents=True, mode=0o755)
    log = []
    try:
        with tempfile.TemporaryDirectory(prefix="scat-cdi-", dir="/opt/scat-nvidia") as temp:
            stage = Path(temp)
            source = stage / "driver"
            p = subprocess.run(
                ["/bin/sh", str(archive), "--extract-only", "--target", str(source)],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            log.append(p.stdout + p.stderr)
            assert p.returncode == 0
            lib = target / "usr/lib/x86_64-linux-gnu"
            lib.mkdir(parents=True)
            for entry in source.glob("*.so*"):
                if entry.is_file() and not entry.is_symlink():
                    shutil.copyfile(entry, lib / entry.name)
                    (lib / entry.name).chmod(0o644)
            (target / "usr/bin").mkdir(parents=True)
            shutil.copyfile(source / "nvidia-smi", target / "usr/bin/nvidia-smi")
            (target / "usr/bin/nvidia-smi").chmod(0o755)
            package = stage / "toolkit"
            subprocess.run(
                ["/usr/bin/dpkg-deb", "--extract", str(deb), str(package)], check=True, timeout=30
            )
            for name in ["nvidia-ctk", "nvidia-cdi-hook"]:
                shutil.copyfile(package / "usr/bin" / name, tools / name)
                (tools / name).chmod(0o755)
        # Required for NVML discovery before the container's vendor hook creates SONAME links.
        for name, actual in {
            "libnvidia-ml.so.1": "libnvidia-ml.so." + VERSION,
            "libcuda.so.1": "libcuda.so." + VERSION,
        }.items():
            (lib / name).symlink_to(actual)
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LD_LIBRARY_PATH": str(lib)}
        p = subprocess.run(
            [
                str(target / "usr/bin/nvidia-smi"),
                "--query-gpu=driver_version,name",
                "--format=csv,noheader",
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        log.append(p.stdout + p.stderr)
        assert p.returncode == 0
        generated = target / "cdi.json"
        p = subprocess.run(
            [
                str(tools / "nvidia-ctk"),
                "cdi",
                "generate",
                "--mode=nvml",
                "--driver-root=" + str(target),
                "--dev-root=/",
                "--nvidia-cdi-hook-path=" + str(tools / "nvidia-cdi-hook"),
                "--format=json",
                "--output=" + str(generated),
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        log.append(p.stdout + p.stderr)
        assert p.returncode == 0
        body = json.loads(generated.read_text())
        assert body["kind"] == "nvidia.com/gpu"
        assert any(d["name"] == "0" for d in body["devices"])
        for mount in body["containerEdits"]["mounts"]:
            assert Path(mount["hostPath"]).resolve().is_relative_to(target)
            assert "ro" in mount["options"]
        spec.parent.mkdir(mode=0o755, exist_ok=True)
        staging = spec.with_suffix(".staging")
        shutil.copyfile(generated, staging)
        staging.chmod(0o644)
        staging.replace(spec)
        report = {
            "status": "installed_candidate_spec",
            "driver_version": VERSION,
            "driver_root": str(target),
            "spec_sha256": sha(spec),
            "hook_sha256": sha(tools / "nvidia-cdi-hook"),
            "host_driver_packages_changed": False,
            "daemon_restarted": False,
            "server_rebooted": False,
            "production_gpu_switched": False,
        }
        (OUT / "cdi-install.json").write_text(json.dumps(report, indent=2) + "\n")
        os.chown(OUT / "cdi-install.json", 1000, 1000)
        print(json.dumps(report))
    finally:
        (OUT / "cdi-install.log").write_text("\n".join(log))
        os.chown(OUT / "cdi-install.log", 1000, 1000)


if __name__ == "__main__":
    main()
