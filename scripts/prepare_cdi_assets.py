"""Fetch vendor-pinned CDI tools and matching userspace libraries; never install a driver.

Only extracts archives inside project staging. No root, kernel, daemon or service changes.
"""

import hashlib
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
ASSETS = ROOT / "var/deploy/gpu/cdi-assets"
TEMP = ROOT / ".tmp/release-readiness/cdi-downloads"
VERSION = "580.173.02"
TOOLKIT = "1.19.1-1"


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, target, timeout=120):
    parsed = urlsplit(url)
    assert parsed.scheme == "https" and parsed.hostname in {
        "download.nvidia.com",
        "nvidia.github.io",
    }
    args = [
        "curl",
        "--fail",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "10",
        "--max-time",
        str(timeout),
    ]
    try:
        socket.getaddrinfo(parsed.hostname, 443)
    except socket.gaierror:
        import dns.resolver

        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = ["223.5.5.5", "119.29.29.29"]
        addresses = [str(x) for x in resolver.resolve(parsed.hostname, "A", lifetime=12)]
        args += ["--resolve", parsed.hostname + ":443:" + addresses[0]]
    part = TEMP / (target.name + ".part")
    result = subprocess.run(
        args + ["--output", str(part), url],
        capture_output=True,
        text=True,
        timeout=timeout + 15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "vendor_download_failed:" + parsed.hostname + ":" + str(result.returncode)
        )
    part.replace(target)


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)
    assert shutil.disk_usage(ROOT).free > 10 * 1024**3
    kernel = Path("/proc/driver/nvidia/version").read_text()
    assert VERSION in kernel, "kernel_changed_review_new_version"
    base = f"https://download.nvidia.com/XFree86/Linux-x86_64/{VERSION}/"
    driver = f"NVIDIA-Linux-x86_64-{VERSION}-no-compat32.run"
    checksum = ASSETS / (driver + ".sha256sum")
    if not checksum.exists():
        fetch(base + checksum.name, checksum)
    expected = checksum.read_text().split()[0]
    assert expected == "e90b79270cfc12fc9374721df4831c6cf0ec332f8ce993c40bb018b0e28bf238"
    package = ASSETS / driver
    if not package.exists():
        fetch(base + driver, package, timeout=900)
    assert sha(package) == expected.lower(), "vendor_driver_hash_mismatch"
    extracted = ASSETS / ("NVIDIA-Linux-x86_64-" + VERSION)
    if not extracted.exists():
        result = subprocess.run(
            ["sh", str(package), "--extract-only", "--target", str(extracted)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        (OUT / "driver-extract.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, "driver_archive_extraction_failed"
    index = ASSETS / "Packages"
    if not index.exists():
        fetch("https://nvidia.github.io/libnvidia-container/stable/deb/amd64/Packages", index)
    candidates = []
    for paragraph in index.read_text().split("\n\n"):
        row = dict(
            line.split(": ", 1)
            for line in paragraph.splitlines()
            if ": " in line and not line.startswith(" ")
        )
        if row.get("Package") == "nvidia-container-toolkit-base" and row.get("Version") == TOOLKIT:
            candidates.append(row)
    assert len(candidates) == 1, "pinned_toolkit_not_available"
    row = candidates[0]
    assert row["SHA256"] == "b6c5b4e77a28cde0197cc0e64edf75538604775d9f8aea502cef667e7e5b2132"
    filename = Path(row["Filename"]).name
    assert filename.endswith(".deb") and re.fullmatch(r"[a-zA-Z0-9_.+-]+", filename)
    deb = ASSETS / filename
    if not deb.exists():
        fetch("https://nvidia.github.io/libnvidia-container/stable/deb/amd64/" + filename, deb)
    assert sha(deb) == row["SHA256"], "toolkit_hash_mismatch"
    toolkit = ASSETS / ("toolkit-" + TOOLKIT)
    if not toolkit.exists():
        subprocess.run(["dpkg-deb", "--extract", str(deb), str(toolkit)], check=True, timeout=30)
    executable = toolkit / "usr/bin/nvidia-ctk"
    help_result = subprocess.run(
        [str(executable), "cdi", "generate", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    (OUT / "cdi-generate-help.txt").write_text(help_result.stdout)
    report = {
        "status": "extracted_only",
        "driver_version": VERSION,
        "driver_archive_sha256": sha(package),
        "driver_source": base + driver,
        "toolkit_version": TOOLKIT,
        "toolkit_sha256": sha(deb),
        "toolkit_source": "https://nvidia.github.io/libnvidia-container/stable/deb/amd64/"
        + filename,
        "host_packages_installed": 0,
        "driver_installed": False,
        "server_rebooted": False,
        "daemon_restarted": False,
    }
    (OUT / "cdi-assets.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
