"""Credential-free reconstruction inputs from the existing Conda environments."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

PUBLIC_HOSTS = {
    "repo.anaconda.com",
    "conda.anaconda.org",
    "mirrors.ustc.edu.cn",
    "mirrors.tuna.tsinghua.edu.cn",
}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")


def pin(name, version):
    if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
        raise ValueError("invalid_package_identity")
    return name.lower().replace("_", "-") + "==" + version


def public_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PUBLIC_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
        or "/t/" in parsed.path
    ):
        raise ValueError("non_public_conda_source")
    return value


def inventory(root):
    root = Path(root).resolve()
    prefix = Path(sys.prefix).resolve()
    if not (prefix / "conda-meta").is_dir() or sys.platform != "linux":
        raise ValueError("existing_linux_conda_required")
    conda = []
    owned = set()
    for path in sorted((prefix / "conda-meta").glob("*.json")):
        row = json.loads(path.read_text())
        checksum = row.get("sha256") or row.get("md5")
        if not isinstance(checksum, str) or not re.fullmatch(
            r"[0-9a-f]{32}|[0-9a-f]{64}", checksum
        ):
            raise ValueError("missing_conda_artifact_checksum")
        conda.append({"url": public_url(row["url"]), "checksum": checksum})
        for member in row.get("files", []):
            if member.endswith((".dist-info/METADATA", ".egg-info/PKG-INFO")):
                if member.startswith("site-packages/"):
                    member = "lib/python" + ".".join(map(str, sys.version_info[:2])) + "/" + member
                owned.add((prefix / member).resolve())
    pip = set()
    local = []
    excluded = []
    for dist in importlib.metadata.distributions():
        package = pin(dist.metadata["Name"], dist.version)
        metadata = Path(dist._path)  # importlib exposes the actual distribution metadata location.
        if (metadata / "METADATA").resolve() in owned or (metadata / "PKG-INFO").resolve() in owned:
            continue
        direct = dist.read_text("direct_url.json")
        if direct:
            source = json.loads(direct)
            parsed = urlsplit(source["url"])
            if parsed.scheme == "file":
                target = Path(parsed.path).resolve()
                if (
                    package == "agent-service-toolkit==0.1.0"
                    and target.name == "agent-service-toolkit-main"
                ):
                    excluded.append(
                        {
                            "package": package,
                            "reason": "legacy_editable_project_not_a_runtime_dependency",
                        }
                    )
                    continue
                if target != root / "vendor/hermes-agent":
                    raise ValueError("unsupported_local_distribution")
                lock = json.loads((root / "services/hermes/upstream.lock.json").read_text())
                local.append(
                    {"package": package, "path": "vendor/hermes-agent", "commit": lock["commit"]}
                )
                continue
            raise ValueError("unsupported_direct_distribution")
        pip.add(package)
    return {
        "schema": 1,
        "python": ".".join(map(str, sys.version_info[:3])),
        "platform": "linux-64",
        "conda": sorted(conda, key=lambda r: r["url"]),
        "pip": sorted(pip),
        "local": sorted(local, key=lambda r: r["package"]),
        "excluded": excluded,
    }


def dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("symlink_refused")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rendered_inputs(snapshot):
    conda = (
        "@EXPLICIT\n" + "\n".join(r["url"] + "#" + r["checksum"] for r in snapshot["conda"]) + "\n"
    )
    requirements = "\n".join(snapshot["pip"]) + "\n"
    return {
        "conda-explicit.txt": conda,
        "pip-requirements.txt": requirements,
        "inventory.json": json.dumps(snapshot, indent=2) + "\n",
    }


def verify_inputs(folder, snapshot):
    return all(
        (Path(folder) / name).read_text() == text
        for name, text in rendered_inputs(snapshot).items()
    )


def write_inputs(folder, snapshot):
    folder = Path(folder)
    for name, text in rendered_inputs(snapshot).items():
        dump(folder / name, text)
    return {
        "conda_packages": len(snapshot["conda"]),
        "pip_packages": len(snapshot["pip"]),
        "local_sources": len(snapshot["local"]),
        "inventory_sha256": hashlib.sha256((folder / "inventory.json").read_bytes()).hexdigest(),
    }
