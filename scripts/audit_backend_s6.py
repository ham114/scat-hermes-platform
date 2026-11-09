"""Post-acceptance audit; optional cleanup is restricted to this project's S6 temporary tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(clean):
    from dotenv import dotenv_values

    from application.skill_jobs import SOURCES
    from hermes_runtime.native_budget import verify_budget_source
    from hermes_runtime.skills import SkillRelease

    assert (Path(sys.prefix) / "conda-meta").is_dir()
    candidate = ROOT / ".tmp/s6"
    temporary = candidate.resolve()
    assert temporary.parent == (ROOT / ".tmp").resolve() and temporary.name == "s6"
    assert not candidate.is_symlink()
    active = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            cwd = (entry / "cwd").resolve()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        joined = b" ".join(args)
        demo_prefixes = (str(ROOT / "var/demo/s5").encode(), b"var/demo/s5")
        if any(
            arg == prefix or arg.startswith(prefix + b"/")
            for arg in args
            for prefix in demo_prefixes
        ):
            continue
        test_prefixes = (str(temporary).encode(), b".tmp/s6")
        owned = any(
            arg == prefix or arg.startswith(prefix + b"/")
            for arg in args
            for prefix in test_prefixes
        )
        driver = any(
            a.endswith(
                (
                    b"evaluate_skills_s6.py",
                    b"run_synthetic_s6_with_dns_cache.py",
                    b"validate_backend_s6.py",
                    b"run_workflow_backend.py",
                    b"evaluate_authoring_s6.py",
                    b"probe_s6_cutover.py",
                )
            )
            for a in args
        )
        if (owned or driver) and (str(ROOT).encode() in joined or cwd == ROOT):
            active.append(int(entry.name))
    ports = {}
    for port in (80, 443, 8080, 8501, 8091, 27017, 18100, 18101, 18102, 18103, 18099, 18080, 18081):
        with socket.socket() as connection:
            connection.settimeout(1)
            ports[str(port)] = connection.connect_ex(("127.0.0.1", port)) == 0
    assert not active and not any(ports[str(p)] for p in (18100, 18101, 18102, 18103)), (
        "S6 still active"
    )
    assert all(ports[str(p)] for p in (80, 443, 8080, 8501, 8091, 27017)), (
        "Original listener changed"
    )
    if clean and temporary.exists():
        shutil.rmtree(temporary)
    mcp = ROOT / ".tmp/mcp"
    if clean and mcp.exists() and not mcp.is_symlink():
        # Hermes exit hooks can leave empty cache skeletons; remove empty directories only.
        for path in sorted(mcp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
        if not any(mcp.iterdir()):
            mcp.rmdir()
    values = dotenv_values(ROOT / ".env")
    private = {
        k: str(v)
        for k, v in values.items()
        if v
        and len(str(v)) >= 8
        and (
            any(s in k.upper() for s in ("SECRET", "PASSWORD", "API_KEY", "ACCESS_TOKEN"))
            or ("URI" in k.upper() and "@" in str(v))
        )
    }
    service_key = ROOT / "var/secrets/hermes-api.key"
    if service_key.is_file():
        private["HERMES_PRIVATE_API_KEY"] = service_key.read_text().strip()
    findings, scanned = [], 0
    for folder in (
        "src",
        "tests",
        "scripts",
        "config",
        "docs",
        "skills",
        "services",
        "authoring",
        "var/validation",
        "var/demo",
    ):
        for path in (ROOT / folder).rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {
                ".py",
                ".md",
                ".json",
                ".yaml",
                ".yml",
                ".log",
                ".toml",
                ".txt",
            }:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scanned += 1
            findings.extend(
                {"path": str(path.relative_to(ROOT)), "key": k}
                for k, v in private.items()
                if v and v in text
            )
    assert not findings, "Private value found; inspect paths only"
    verify_budget_source(ROOT, ROOT / "vendor/hermes-agent")
    prior = json.loads((ROOT / "docs/validation/20261023-S3-server-cleanup.json").read_text())
    oracle = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in prior["old_oracle_sha256"]
    }
    assert oracle == prior["old_oracle_sha256"]
    mode = stat.S_IMODE((ROOT / ".env").stat().st_mode)
    assert mode == 0o600
    assert not any((ROOT / name).exists() for name in (".venv", "venv"))
    release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
    report = {
        "status": "passed",
        "demo_runtime_excluded_from_test_cleanup": "var/demo/s5 and .tmp/s5-demo",
        "active_s6_processes": active,
        "ports_listening": ports,
        "text_files_scanned": scanned,
        "private_value_findings": findings,
        "dotenv_mode": format(mode, "04o"),
        "project_venv": False,
        "conda_python": sys.executable,
        "temporary_s6_removed": not temporary.exists(),
        "old_oracle_sha256": oracle,
        "bundle_version": release.manifest["bundle_version"],
        "bundle_sha256": release.bundle_sha256,
        "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES},
    }
    path = ROOT / "docs/validation/20261023-S6-server-cleanup.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true")
    main(parser.parse_args().clean)
