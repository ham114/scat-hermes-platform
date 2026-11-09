"""Finalize finite Honcho tests, stop only owned services, and export safe evidence."""

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/honcho"
OUT = ROOT / "var/validation/20261027-honcho"


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    from dotenv import dotenv_values

    os.umask(0o077)
    first = read(OUT / "memory/result.json")
    auth = read(OUT / "memory/auth-recheck.json")
    checks = dict(first["checks"])
    if auth["status"] != "passed":
        raise ValueError("authentication_recheck_missing")
    checks.update(auth["checks"])
    lifecycle = read(OUT / "lifecycle.json")
    checks.update(lifecycle["checks"])
    provider = read(OUT / "provider-result.json")
    checks["provider_checks"] = provider["status"] == "passed"
    suites = list(ET.parse(OUT / "target-tests.xml").getroot().iter("testsuite"))
    tests = {
        name: sum(int(suite.get(name, 0)) for suite in suites)
        for name in ["tests", "failures", "errors", "skipped"]
    }
    checks["targeted_tests"] = tests["tests"] == 18 and tests["failures"] == tests["errors"] == 0
    with sqlite3.connect(DEPLOY / "testing/proxy-budget.sqlite") as db:
        db.row_factory = sqlite3.Row
        attempts = [
            dict(row)
            for row in db.execute(
                "SELECT id,reserved,status,input_tokens,output_tokens FROM requests ORDER BY id"
            )
        ]
    requests = provider["model_requests"] + len(attempts)
    conservative = provider["conservative_reserved"] + sum(row["reserved"] for row in attempts)
    inp = sum(row["usage"]["prompt_tokens"] for row in provider["attempts"]) + sum(
        row["input_tokens"] for row in attempts
    )
    out = sum(row["usage"]["completion_tokens"] for row in provider["attempts"]) + sum(
        row["output_tokens"] for row in attempts
    )
    checks["same_campaign_budget_including_failures"] = requests <= 60 and conservative <= 500000
    before = read(ROOT / "var/validation/20261027-h7b/preflight.json")
    checks["existing_conda_histories_unchanged"] = all(
        digest(Path("/home/h3c/anaconda3/envs") / name / "conda-meta/history") == value
        for name, value in before["history"].items()
    )
    previous = read(ROOT / "docs/validation/20261027-h7b-acceptance.json")
    expected = {row["service"]: row for row in previous["containers"]}
    raw_ids = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=scat-hermes-h7b"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    preserved = []
    for identity in raw_ids:
        value = json.loads(
            subprocess.run(
                ["docker", "inspect", identity], capture_output=True, text=True, check=True
            ).stdout
        )[0]
        service = value["Config"]["Labels"]["com.docker.compose.service"]
        preserved.append(
            value["Id"][:12] == expected[service]["id"]
            and value["Image"] == expected[service]["image_id"]
            and value["State"].get("Health", {}).get("Status") == "healthy"
        )
    checks["seven_formal_cpu_containers_preserved"] = len(preserved) == 7 and all(preserved)
    ports = {}
    for port in [80, 443, 8080, 8501, 8091, 27017, 18081, 18133]:
        with socket.socket() as sock:
            sock.settimeout(2)
            ports[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
    checks["existing_service_ports_preserved"] = all(ports.values())
    rag = read(ROOT / "services/rag/qwen3-production-v1.json")
    checks["formal_corpus_and_vectors_preserved"] = (
        digest(Path(rag["corpus"])) == rag["corpus_sha256"]
        and digest(Path(rag["embedding_cache"])) == rag["embedding_cache_sha256"]
    )
    secrets = set()
    for path in (DEPLOY / "secrets").glob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".key":
            secrets.add(path.read_bytes().strip())
        elif path.suffix == ".env":
            for name, value in dotenv_values(path).items():
                if (
                    value
                    and len(value) >= 20
                    and (
                        name.endswith(("_API_KEY", "_SECRET", "_PASSWORD"))
                        or name == "DB_CONNECTION_URI"
                    )
                ):
                    secrets.add(value.encode())
    paths = set((ROOT / "config/honcho").rglob("*"))
    paths.update(
        ROOT / "scripts" / name
        for name in [
            "probe_honcho_provider.py",
            "prepare_honcho_test_source.py",
            "build_honcho_test_image.py",
            "run_honcho_test_proxy.py",
            "serve_honcho_embeddings.py",
            "manage_honcho_test.py",
            "probe_honcho_memory.py",
            "verify_honcho_test_lifecycle.py",
            "audit_honcho_test.py",
            "prepare_container_context.py",
        ]
    )
    paths.update(OUT.rglob("*"))
    paths.update((ROOT / "docs").glob("*HONCHO*"))
    paths.update((DEPLOY / "image-context/app").rglob("*"))
    exposed = []
    scanned = 0
    for path in paths:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 8 * 1024**2:
            scanned += 1
            if any(value in path.read_bytes() for value in secrets):
                exposed.append(str(path.relative_to(ROOT)))
    checks["current_public_files_private_value_scan"] = not exposed
    subprocess.run(
        [sys.executable, "-B", "scripts/manage_honcho_test.py", "stop"],
        cwd=ROOT,
        check=True,
        timeout=120,
    )
    for _ in range(30):
        open_ports = []
        for port in [18140, 18141, 18142]:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    open_ports.append(port)
        if not open_ports:
            break
        time.sleep(0.5)
    checks["finite_candidate_listeners_closed"] = not open_ports
    active = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=scat.owner=scat-honcho-test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    checks["finite_candidate_containers_stopped"] = not active
    if checks["finite_candidate_listeners_closed"]:
        for target in [ROOT / ".tmp/honcho-test", DEPLOY / "testing/plugin-home"]:
            if target.exists():
                if (
                    target.is_symlink()
                    or not target.resolve().is_relative_to(ROOT.resolve())
                    or target.resolve() == ROOT.resolve()
                ):
                    raise ValueError("unsafe_cleanup_path")
                shutil.rmtree(target)
    checks["temporary_files_and_test_profile_removed"] = (
        not (ROOT / ".tmp/honcho-test").exists() and not (DEPLOY / "testing/plugin-home").exists()
    )
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "tests": tests,
        "model_request_attempts_including_failures": requests,
        "successful_model_responses": len(provider["attempts"])
        + sum(row["status"] == "ok" for row in attempts),
        "conservative_reserved": conservative,
        "provider_input_tokens": inp,
        "provider_output_tokens": out,
        "provider_total_tokens": inp + out,
        "internal_attempts": attempts,
        "image": read(DEPLOY / "image.lock.json"),
        "credential_scan": {"files": scanned, "exposure_files": exposed},
        "scope": "synthetic Honcho server/SDK/Hermes plugin lifecycle; formal platform integration not activated",
        "initial_assertion_failure_retained": "memory/result.json; SDK uses status instead of status_code",
        "auth_recheck_new_paid_calls": 0,
        "formal_platform_memory_enabled": False,
        "historical_import": False,
        "new_host_environments": 0,
        "host_dependency_installs": 0,
        "earlier_credential_exposure": "public JSON field entered tool output; field cleared and server private provider file populated; rotation recommended",
    }
    (ROOT / "docs/validation/20261027-honcho-acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"internal_attempts", "image"}
            },
            ensure_ascii=False,
        )
    )
    assert all(checks.values()), "honcho_final_audit_failed"


if __name__ == "__main__":
    main()
