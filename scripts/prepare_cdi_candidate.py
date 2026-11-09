"""Build a project-local matching userspace root and vendor-generated CDI candidate."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "var/deploy/gpu/cdi-assets"
OUT = ROOT / "var/validation/20261108-release-readiness"
VERSION = "580.173.02"


def main():
    assert VERSION in Path("/proc/driver/nvidia/version").read_text()
    report = json.loads((OUT / "cdi-assets.json").read_text())
    assert (
        report["driver_archive_sha256"]
        == "e90b79270cfc12fc9374721df4831c6cf0ec332f8ce993c40bb018b0e28bf238"
    )
    extracted = BASE / ("NVIDIA-Linux-x86_64-" + VERSION)
    target = BASE / ("driver-root-" + VERSION)
    lib = target / "usr/lib/x86_64-linux-gnu"
    lib.mkdir(parents=True, exist_ok=True)
    binary = target / "usr/bin"
    binary.mkdir(parents=True, exist_ok=True)
    hashes = {}
    # Only userspace libraries and diagnostic binary; no kernel/installer execution.
    for source in sorted(extracted.glob("*.so*")):
        if not source.is_file() or source.is_symlink():
            continue
        destination = lib / source.name
        shutil.copyfile(source, destination)
        hashes[source.name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    for name, actual in {
        "libcuda.so.1": "libcuda.so." + VERSION,
        "libnvidia-ml.so.1": "libnvidia-ml.so." + VERSION,
        "libnvidia-ptxjitcompiler.so.1": "libnvidia-ptxjitcompiler.so." + VERSION,
        "libnvidia-nvvm.so.4": "libnvidia-nvvm.so." + VERSION,
    }.items():
        assert (lib / actual).is_file(), actual
        link = lib / name
        if not link.exists():
            link.symlink_to(actual)
    shutil.copyfile(extracted / "nvidia-smi", binary / "nvidia-smi")
    (binary / "nvidia-smi").chmod(0o755)
    env = os.environ | {"LD_LIBRARY_PATH": str(lib)}
    probe = subprocess.run(
        [
            str(binary / "nvidia-smi"),
            "--query-gpu=driver_version,name,uuid",
            "--format=csv,noheader",
        ],
        env=env,
        text=True,
        check=False,
        capture_output=True,
        timeout=15,
    )
    (OUT / "matching-userspace-probe.log").write_text(probe.stdout + probe.stderr)
    assert probe.returncode == 0, "matching_userspace_nvml_unavailable"
    toolkit = BASE / "toolkit-1.19.1-1/usr/bin"
    result = subprocess.run(
        [
            str(toolkit / "nvidia-ctk"),
            "cdi",
            "generate",
            "--mode=nvml",
            "--driver-root=" + str(target),
            "--dev-root=/",
            "--nvidia-cdi-hook-path=/usr/local/libexec/scat-nvidia/nvidia-cdi-hook",
            "--format=json",
            "--output=" + str(OUT / "cdi-candidate.json"),
        ],
        env=env,
        text=True,
        check=False,
        capture_output=True,
        timeout=30,
    )
    (OUT / "cdi-generate.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, "vendor_cdi_generate_failed"
    body = json.loads((OUT / "cdi-candidate.json").read_text())
    assert body["kind"] == "nvidia.com/gpu" and body["devices"]
    (OUT / "cdi-driver-root.json").write_text(
        json.dumps(
            {
                "status": "candidate_only",
                "driver_version": VERSION,
                "libraries": hashes,
                "devices": [d["name"] for d in body["devices"]],
                "root": str(target),
                "host_driver_changed": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "status": "candidate_only",
                "devices": [d["name"] for d in body["devices"]],
                "libraries": len(hashes),
            }
        )
    )


if __name__ == "__main__":
    main()
