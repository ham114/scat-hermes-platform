"""Attest standard profiles and clean owned test files; emit fixed metadata only."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(driver_id):
    from dotenv import dotenv_values

    from application.skill_jobs import SOURCES
    from hermes_runtime.profiles import ROLES, metadata, profile_home
    from hermes_runtime.release_store import resolve_release

    if len(driver_id) != 10 or any(c not in "0123456789abcdef" for c in driver_id):
        raise ValueError("invalid_driver_id")
    driver = json.loads(
        (
            ROOT / "docs/validation" / ("20261024-profiles-" + driver_id + "-backend-driver.json")
        ).read_text()
    )
    runtime = json.loads((ROOT / "docs/validation/20261024-profiles-runtime.json").read_text())
    native = json.loads((ROOT / "docs/validation/20261024-profiles-native.json").read_text())
    assert driver["status"] == runtime["status"] == native["status"] == "passed"
    assert runtime["synthetic_only"] is True and all(
        runtime["profile_cases"][name]["passed"] for name in ("main", "worker", "author")
    )
    assert runtime["profile_cases"]["author"]["profile_manage_called"]
    assert len(driver["checks"]) == 6 and all(row["exit"] == 0 for row in driver["checks"])
    suite = (
        ET.parse(ROOT / "var/validation" / ("20261024-profiles-" + driver_id + "-pytest.xml"))
        .getroot()
        .find("testsuite")
    )
    tests = {key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")}
    assert tests["failures"] == tests["errors"] == 0
    records = []
    provider_keys = []
    for name in ROLES:
        home = profile_home(ROOT, name)
        info = metadata(ROOT, name)
        env = dotenv_values(home / ".env")
        assert set(env) == {"DEEPSEEK_API_KEY"} and env["DEEPSEEK_API_KEY"]
        assert (home / ".env").stat().st_mode & 0o077 == 0
        assert info["directories_valid"] and info["state_db_exists"]
        assert all(
            (home / item).is_file()
            for item in (
                "SOUL.md",
                "AGENTS.md",
                "profile.yaml",
                "memories/MEMORY.md",
                "memories/USER.md",
            )
        )
        provider_keys.append(env["DEEPSEEK_API_KEY"])
        records.append(
            {
                "name": name,
                "role": ROLES[name],
                "home": info["home"],
                "layout_valid": True,
                "provider_key_configured": True,
                "env_private": True,
                "state_db_exists": True,
            }
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
            str(ROOT / ".tmp/profiles").encode() in arg or arg.startswith(b".tmp/profiles/")
            for arg in args
        ):
            processes.append(int(path.name))
    assert not processes, "owned_profile_processes_remaining"
    ports = {}
    for port in (80, 443, 8080, 8501, 8091, 27017, 18108, 18109, 18110, 18111, 18112):
        with socket.socket() as sock:
            sock.settimeout(1)
            ports[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
    assert all(ports[str(port)] for port in (80, 443, 8080, 8501, 8091, 27017))
    assert not any(ports[str(port)] for port in (18108, 18109, 18110, 18111, 18112))
    baseline = json.loads((ROOT / "docs/validation/20261024-S7A-sync.json").read_text())
    frontend = [
        row for row in baseline["source_and_evidence_files"] if row["path"].startswith("frontend/")
    ]
    assert all(sha(ROOT / row["path"]) == row["sha256"] for row in frontend)
    release = resolve_release(ROOT)
    assert (
        release.bundle_sha256 == "f713ad1f165cb7ffeb0a324503a35697e643fa51e707d8f194ebd343e10c1d95"
    )
    private = provider_keys + [
        (ROOT / "var/secrets" / name).read_text().strip()
        for name in ("hermes-api.key", "skill-author.key", "skill-release.key")
    ]
    env = dotenv_values(ROOT / ".env")
    private.extend(
        value
        for key, value in env.items()
        if key in {"AUTH_SECRET", "FLIGHT_MONGO_URI"} and value and len(value) >= 12
    )
    scanned = findings = 0
    for folder in ("docs", "src", "scripts", "var/validation"):
        for path in (ROOT / folder).rglob("*"):
            if (
                path.is_file()
                and path.suffix in {".py", ".md", ".json", ".log", ".xml"}
                and path.stat().st_size <= 16 * 1024 * 1024
            ):
                scanned += 1
                text = path.read_text(errors="replace")
                findings += sum(value in text for value in set(private) if value)
    assert findings == 0, "private_values_detected_on_server"
    work = ROOT / ".tmp/profiles"
    links = 0
    if work.exists():
        assert work.resolve() == ROOT.resolve() / ".tmp/profiles" and not work.is_symlink()
        for path in work.rglob("*"):
            if path.is_symlink():
                path.unlink()
                links += 1
            elif path.is_file():
                path.chmod(0o600)
        shutil.rmtree(work)
    legacy_empty = ROOT / ".tmp/s7a"
    if legacy_empty.is_dir() and not legacy_empty.is_symlink() and not any(legacy_empty.iterdir()):
        legacy_empty.rmdir()
    author = runtime["profile_cases"]["author"]
    result = {
        "status": "passed",
        "date": "2026-10-24",
        "stage": "standard-profiles",
        "tests": tests,
        "passed_tests": tests["tests"] - tests["skipped"],
        "checks": driver["checks"],
        "profiles": records,
        "native_profile_count": 3,
        "distinct_provider_keys": len(set(provider_keys)),
        "main_synthetic_query_passed": runtime["profile_cases"]["main"]["passed"] is True,
        "worker_approved_cpu_passed": runtime["profile_cases"]["worker"]["passed"] is True,
        "worker_actual_backend_calls": int(
            runtime["profile_cases"]["worker"]["actual_backend_calls"]
        ),
        "author_draft_creation_passed": author["passed"] is True,
        "author_profile_manage_called": author["profile_manage_called"] is True,
        "author_temperature": float(author["temperature"]),
        "author_llm_calls": int(author["llm_calls"]),
        "author_tool_calls": int(author["tool_calls"]),
        "author_publication_events": int(author["publication_events"]),
        "flow_usage": runtime["flow_usage"],
        "production_bundle_version": release.manifest["bundle_version"],
        "production_bundle_sha256": release.bundle_sha256,
        "frontend_files_unchanged": len(frontend),
        "owned_processes_remaining": 0,
        "ports_listening": ports,
        "text_files_scanned": scanned,
        "private_value_findings": findings,
        "temporary_links_unlinked": links,
        "temporary_profiles_removed": not work.exists(),
        "source_sha256": {name: sha(ROOT / name) for name in SOURCES},
        "raw_model_reports_remain_on_server": True,
    }
    (ROOT / "docs/validation/20261024-profiles-acceptance.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "passed_tests": result["passed_tests"],
                "native_profiles": 3,
                "extra_provider_keys_required": 0,
                "temporary_removed": result["temporary_profiles_removed"],
            }
        )
    )


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing app Anaconda")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True)
    args = parser.parse_args()
    main(args.driver)
