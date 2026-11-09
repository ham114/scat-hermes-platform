"""H7-A fixed metadata attestation and cleanup after all owned processes have stopped."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SOURCES = [
    "src/backend_ops/__init__.py",
    "src/backend_ops/environment.py",
    "src/backend_ops/runtime.py",
    "scripts/manage_backend.py",
    "scripts/manage_backend_environment.py",
    "scripts/probe_backend_operations.py",
    "scripts/validate_backend_operations.py",
    "scripts/finalize_backend_operations.py",
    "tests/hermes_runtime/test_backend_operations.py",
    "config/backend-h7a.json",
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    from dotenv import dotenv_values

    from backend_ops.environment import dump
    from backend_ops.runtime import APP, process_identity
    from hermes_runtime.release_store import resolve_release

    if sys.executable != APP:
        raise ValueError("existing_scat_agent_required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks", required=True)
    parser.add_argument("--lifecycle", required=True)
    args = parser.parse_args()
    for value in (args.checks, args.lifecycle):
        if not re.fullmatch(
            r"docs/validation/20261024-H7A-[0-9a-f]+-(checks|lifecycle)\.json", value
        ):
            raise ValueError("fixed_metadata_report_required")
    driver = json.loads((ROOT / args.checks).read_text())
    lifecycle = json.loads((ROOT / args.lifecycle).read_text())
    assert driver["status"] == lifecycle["status"] == "passed"
    assert driver["pytest"]["failures"] == driver["pytest"]["errors"] == 0
    assert all(row["exit"] == 0 for row in driver["checks"])
    assert lifecycle["ports_closed"] and lifecycle["owned_state_removed"]
    assert lifecycle["isolated_collections_removed"] and all(lifecycle["checks"].values())
    assert lifecycle["llm_calls"] == 0
    processes = []
    entries = {
        str(ROOT / "scripts" / name).encode()
        for name in (
            "manage_backend.py",
            "probe_backend_operations.py",
            "validate_backend_operations.py",
            "run_platform_api.py",
            "run_hermes_api.py",
            "run_model_worker.py",
            "run_scat_mcp.py",
            "run_skill_cpu_backend.py",
            "run_workflow_backend.py",
        )
    }
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal() or int(path.name) == os.getpid():
            continue
        try:
            args_bytes = set((path / "cmdline").read_bytes().split(b"\0"))
            if args_bytes & entries and process_identity(int(path.name)):
                processes.append(int(path.name))
        except OSError:
            continue
    assert not processes, "owned_h7a_processes_remaining"
    ports = {}
    for port in (80, 443, 8080, 8501, 8091, 27017, 18120, 18121, 18122):
        with socket.socket() as sock:
            sock.settimeout(1)
            ports[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
    assert all(ports[str(p)] for p in (80, 443, 8080, 8501, 8091, 27017))
    assert not any(ports[str(p)] for p in (18120, 18121, 18122))
    previous = json.loads((ROOT / "docs/validation/20261024-profiles-acceptance.json").read_text())
    assert all(sha(ROOT / p) == digest for p, digest in previous["source_sha256"].items())
    baseline = json.loads((ROOT / "docs/validation/20261024-S7A-sync.json").read_text())
    frontend = [
        r for r in baseline["source_and_evidence_files"] if r["path"].startswith("frontend/")
    ]
    assert all(sha(ROOT / r["path"]) == r["sha256"] for r in frontend)
    release = resolve_release(ROOT)
    assert release.bundle_sha256 == previous["production_bundle_sha256"]
    private = []
    for name in ("scat-main", "scat-worker", "scat-author"):
        env = ROOT / "var/hermes/managed/profiles" / name / ".env"
        assert env.stat().st_mode & 0o077 == 0
        private.append(dotenv_values(env).get("DEEPSEEK_API_KEY"))
    for name in ("hermes-api.key", "skill-author.key", "skill-release.key"):
        private.append((ROOT / "var/secrets" / name).read_text().strip())
    values = dotenv_values(ROOT / ".env")
    private.extend(values.get(k) for k in ("DEEPSEEK_API_KEY", "AUTH_SECRET", "FLIGHT_MONGO_URI"))
    private = {v for v in private if v and len(v) >= 12}
    scanned = findings = 0
    for folder in ("src", "scripts", "docs", "config", "environments", "var/validation"):
        for path in (ROOT / folder).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix in {".py", ".md", ".json", ".txt", ".log", ".xml"}
                and path.stat().st_size <= 16 * 1024 * 1024
            ):
                scanned += 1
                text = path.read_text(errors="replace")
                findings += sum(value in text for value in private)
    assert findings == 0, "private_value_detected"
    work = ROOT / ".tmp/h7a"
    links = 0
    if work.exists():
        assert work.resolve() == ROOT.resolve() / ".tmp/h7a" and not work.is_symlink()
        for path in work.rglob("*"):
            if path.is_symlink():
                path.unlink()
                links += 1
        shutil.rmtree(work)
    inputs = sorted((ROOT / "environments/backend-h7a").rglob("*"))
    result = {
        "status": "passed",
        "date": "2026-10-24",
        "stage": "H7-A",
        "checks_report": args.checks,
        "lifecycle_report": args.lifecycle,
        "pytest": driver["pytest"],
        "passed_tests": driver["pytest"]["tests"] - driver["pytest"]["skipped"],
        "checks": driver["checks"],
        "lifecycle_checks": lifecycle["checks"],
        "llm_calls": 0,
        "installs": 0,
        "new_environments": 0,
        "production_gpu": False,
        "business_execution_sources_unchanged": len(previous["source_sha256"]),
        "frontend_files_unchanged": len(frontend),
        "production_bundle_version": release.manifest["bundle_version"],
        "production_bundle_sha256": release.bundle_sha256,
        "ports_listening": ports,
        "owned_processes_remaining": 0,
        "temporary_removed": not work.exists(),
        "temporary_links_unlinked": links,
        "text_files_scanned": scanned,
        "private_value_findings": findings,
        "source_sha256": {p: sha(ROOT / p) for p in SOURCES},
        "dependency_inputs_sha256": {
            p.relative_to(ROOT).as_posix(): sha(p) for p in inputs if p.is_file()
        },
        "fresh_environment_rebuild": False,
        "compose_deployed": False,
        "raw_logs_remain_on_server": True,
    }
    dump(ROOT / "docs/validation/20261024-H7A-acceptance.json", json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "passed_tests": result["passed_tests"],
                "lifecycle_checks": len(result["lifecycle_checks"]),
                "private_value_findings": findings,
                "temporary_removed": result["temporary_removed"],
            }
        )
    )


if __name__ == "__main__":
    main()
