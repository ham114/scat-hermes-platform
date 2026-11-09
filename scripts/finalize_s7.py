"""Bounded server attestation and owned temporary cleanup; export metadata only."""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_evidence(name):
    if not re.fullmatch(r"20261024-S7A-[a-zA-Z0-9.-]+\.json", name):
        raise ValueError("invalid_evidence_filename")
    return json.loads((ROOT / "docs/validation" / name).read_text())


def main(driver_name, lifecycle_name, cleanup):
    from dotenv import dotenv_values

    from application.skill_jobs import SOURCES
    from hermes_runtime.release_store import ReleaseStore

    driver, lifecycle = read_evidence(driver_name), read_evidence(lifecycle_name)
    assert driver["status"] == lifecycle["status"] == "passed"
    assert len(driver["checks"]) == 6 and all(row["exit"] == 0 for row in driver["checks"])
    assert lifecycle["synthetic_only"] is True and lifecycle["production_publication"] is False
    report = lifecycle["trial_report"]
    assert report["status"] == "passed" and report["synthetic_only"] is True
    assert report["temporary_home_removed"] is True and report["owned_processes_remaining"] == 0
    xml = ROOT / "var/validation" / ("20261024-S7A-" + driver["identity"] + "-pytest.xml")
    suite = ET.parse(xml).getroot().find("testsuite")
    tests = {k: int(suite.attrib[k]) for k in ("tests", "errors", "failures", "skipped")}
    assert tests["errors"] == tests["failures"] == 0
    release = ReleaseStore(ROOT).current()
    release.verify_native_source(ROOT / "vendor/hermes-agent")
    assert (
        release.bundle_sha256 == "f713ad1f165cb7ffeb0a324503a35697e643fa51e707d8f194ebd343e10c1d95"
    )
    processes = []
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal() or int(path.name) == os.getpid():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            args = (path / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(
            (str(ROOT / ".tmp/s7a").encode() in arg or arg.startswith(b".tmp/s7a/")) for arg in args
        ) or any(
            arg.endswith(
                (
                    b"/run_skill_author_api.py",
                    b"/run_skill_author_trial.py",
                    b"/probe_authoring_s7.py",
                )
            )
            or arg
            in {
                b"scripts/run_skill_author_api.py",
                b"scripts/run_skill_author_trial.py",
                b"scripts/probe_authoring_s7.py",
            }
            for arg in args
        ):
            processes.append(int(path.name))
    assert not processes, "S7 owned processes still active"
    ports = {}
    for port in (80, 443, 8080, 8501, 8091, 27017, 18104, 18105, 18106, 18107):
        with socket.socket() as sock:
            sock.settimeout(1)
            ports[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
    assert all(ports[str(port)] for port in (80, 443, 8080, 8501, 8091, 27017))
    assert not any(ports[str(port)] for port in (18104, 18105, 18106, 18107))
    old = json.loads((ROOT / "docs/validation/20261023-S6-sync.json").read_text())
    frontend = [
        row for row in old["source_and_evidence_files"] if row["path"].startswith("frontend/")
    ]
    assert frontend and all(sha(ROOT / row["path"]) == row["sha256"] for row in frontend)
    previous = json.loads((ROOT / "docs/validation/20261023-S6-server-cleanup.json").read_text())
    assert all(
        sha(ROOT / path) == expected for path, expected in previous["old_oracle_sha256"].items()
    )
    values = dotenv_values(ROOT / ".env")
    private_values = [
        value
        for key, value in values.items()
        if value
        and len(value) >= 12
        and any(part in key.upper() for part in ("KEY", "SECRET", "PASSWORD", "MONGO_URI"))
    ]
    findings, count = 0, 0
    for folder in (ROOT / "docs", ROOT / "var/validation", ROOT / "src", ROOT / "scripts"):
        for path in folder.rglob("*"):
            if (
                path.is_file()
                and path.suffix in {".json", ".log", ".xml", ".md", ".py"}
                and path.stat().st_size <= 16 * 1024 * 1024
            ):
                count += 1
                text = path.read_text(errors="replace")
                findings += sum(value in text for value in private_values)
    assert findings == 0, "Private value findings stay on server for remediation"
    target = ROOT / ".tmp/s7a"
    links_removed = 0
    if cleanup and target.exists():
        assert target.resolve() == ROOT.resolve() / ".tmp/s7a" and not target.is_symlink()
        for path in target.rglob("*"):
            if path.is_symlink():
                # Tests deliberately create escaping links. Remove only the link
                # entry under the verified temporary root, never chmod its target.
                path.unlink()
                links_removed += 1
                continue
            if path.is_file():
                path.chmod(0o600)
        shutil.rmtree(target)
    attestation = {
        "status": "passed",
        "stage": "S7-A",
        "date": "2026-10-24",
        "backend_driver": driver_name,
        "lifecycle_report": lifecycle_name,
        "tests": tests,
        "passed_tests": tests["tests"] - tests["skipped"],
        "checks": driver["checks"],
        "synthetic_trial": {
            "attempts": len(report["attempts"]),
            "passed": sum(row["passed"] is True for row in report["attempts"]),
            "domains": {
                name: {k: value[k] for k in ("passed", "total", "completion_rate", "gate_passed")}
                for name, value in report["summary"]["domains"].items()
            },
            "usage": {
                k: report["summary"][k]
                for k in (
                    "llm_calls",
                    "tool_calls",
                    "conservative_tokens",
                    "provider_input_tokens",
                    "provider_output_tokens",
                    "provider_cached_tokens",
                )
            },
        },
        "native_routing": read_evidence("20261024-S7A-native-routing.json"),
        "interrupted_trial": read_evidence("20261024-S7A-interrupted-trial.json"),
        "publication_replay_and_rollback": all(
            lifecycle.get(k) is True
            for k in (
                "independent_release_authority",
                "confirmation_replay_idempotent",
                "run_pins_preserved",
                "rollback_passed",
            )
        ),
        "production_bundle_version": release.manifest["bundle_version"],
        "production_bundle_sha256": release.bundle_sha256,
        "production_skill_publication": False,
        "frontend_files_unchanged": len(frontend),
        "original_oracle_unchanged": True,
        "owned_processes_remaining": 0,
        "ports_listening": ports,
        "temporary_s7a_removed": not target.exists(),
        "temporary_links_unlinked_without_following": links_removed,
        "private_text_files_scanned": count,
        "private_value_findings": findings,
        "dotenv_mode": oct((ROOT / ".env").stat().st_mode & 0o777),
        "project_venv": any((ROOT / name).exists() for name in ("venv", ".venv")),
        "source_sha256": {name: sha(ROOT / name) for name in SOURCES},
        "raw_model_and_test_reports_remain_on_server": True,
    }
    assert attestation["publication_replay_and_rollback"] and not attestation["project_venv"]
    destination = ROOT / "docs/validation/20261024-S7A-acceptance.json"
    destination.write_text(json.dumps(attestation, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "passed_tests": attestation["passed_tests"],
                "skipped": tests["skipped"],
                "trial_attempts": attestation["synthetic_trial"]["attempts"],
                "temporary_removed": attestation["temporary_s7a_removed"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True)
    parser.add_argument("--lifecycle", required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing app Anaconda")
    main(args.driver, args.lifecycle, args.cleanup)
