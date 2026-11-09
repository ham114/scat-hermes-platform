"""Read-only deployment evidence and private-value audit; exports contain no keys."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261027-h7b"
DEPLOY = ROOT / "var/deploy/h7b"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=60
    ).stdout


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    os.umask(0o077)
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
    from dotenv import dotenv_values

    from hermes_runtime.native_budget import verify_budget_source
    from hermes_runtime.native_profiles import verify_native

    checks = {}
    baseline = read(OUT / "preflight.json")
    checks["three_existing_conda_histories_unchanged"] = all(
        sha(Path("/home/h3c/anaconda3/envs") / name / "conda-meta/history") == value
        for name, value in baseline["history"].items()
    )
    verify_native(ROOT)
    verify_budget_source(ROOT, ROOT / "vendor/hermes-agent")
    checks["vendor_and_budget_pins_unchanged"] = True
    for port in [80, 443, 8080, 8501, 8091, 27017, 18081, 18133]:
        with socket.socket() as sock:
            sock.settimeout(3)
            checks["existing_port_" + str(port)] = sock.connect_ex(("127.0.0.1", port)) == 0
    legacy = json.loads(docker("inspect", "scat-gbar-production-gbar-1"))[0]
    checks["legacy_gbar_preserved"] = (
        legacy["Image"] == baseline["gbar_existing_image"]
        and legacy["State"].get("Health", {}).get("Status") == "healthy"
    )
    config = read(ROOT / "services/rag/qwen3-production-v1.json")
    checks["formal_corpus_preserved"] = (
        sha(Path(config["corpus"])) == config["corpus_sha256"]
        and sha(Path(config["embedding_cache"])) == config["embedding_cache_sha256"]
    )
    old = read(ROOT / "services/rag/qwen3-06b-dev40-v1.json")
    checks["old_frozen_test_inputs_preserved"] = all(
        sha(Path(row["path"])) == row["sha256"] for row in old["inputs"].values()
    )
    suite = list(ET.parse(OUT / "full-tests.xml").getroot().iter("testsuite"))
    tests = {
        key: sum(int(row.get(key, 0)) for row in suite)
        for key in ["tests", "failures", "errors", "skipped"]
    }
    checks["full_regression"] = (
        tests["failures"] == tests["errors"] == 0 and tests["tests"] - tests["skipped"] >= 1302
    )
    checks["readonly_integration"] = read(OUT / "integration-readonly.json")["status"] == "passed"
    workflow = read(OUT / "workflow-network-v2/workflow-result.json")
    checks["real_main_child_workflow"] = workflow["status"] == "passed"
    checks["recreation_and_unknown_no_replay"] = read(OUT / "lifecycle.json")["status"] == "passed"
    locks = read(DEPLOY / "images.lock.json")
    role_images = {row["image_id"] for row in locks.values()}
    ids = docker(
        "ps", "-aq", "--filter", "label=com.docker.compose.project=scat-hermes-h7b"
    ).splitlines()
    rows = []
    credentials = set()
    for path in [
        ROOT / ".env",
        *list((DEPLOY / "profile-roots").rglob(".env")),
        *list((DEPLOY / "secrets").glob("*.env")),
    ]:
        for name, value in dotenv_values(path).items():
            if (
                value
                and len(value) >= 24
                and any(
                    term in name.upper()
                    for term in ["KEY", "SECRET", "TOKEN", "PASSWORD", "MONGO_URI"]
                )
            ):
                credentials.add(value.encode())
    for path in (DEPLOY / "secrets").glob("*.key"):
        credentials.add(path.read_bytes().strip())
    checks["private_files_0600"] = all(
        not path.is_symlink() and path.stat().st_mode & 0o077 == 0
        for path in [
            *list((DEPLOY / "secrets").iterdir()),
            *list((DEPLOY / "profile-roots").rglob(".env")),
        ]
        if path.is_file()
    )
    scan = set((DEPLOY / "context/app").rglob("*"))
    scan.update((ROOT / "infra/compose").rglob("*"))
    scan.update((OUT).rglob("*"))
    scan.update((ROOT / "docs").glob("*H7B*"))
    scanned = 0
    exposures = []
    for path in scan:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024**2:
            continue
        scanned += 1
        if any(value in path.read_bytes() for value in credentials):
            exposures.append(str(path.relative_to(ROOT)))
    # Docker health can lag HTTP recovery by one check interval after pause.
    # Wait for the declared health checks; do not silently accept unhealthy.
    for _ in range(12):
        if all(
            json.loads(docker("inspect", identity))[0]["State"].get("Health", {}).get("Status")
            == "healthy"
            for identity in ids
        ):
            break
        time.sleep(5)
    for identity in ids:
        item = json.loads(docker("inspect", identity))[0]
        image = json.loads(docker("image", "inspect", item["Image"]))[0]
        service = item["Config"]["Labels"]["com.docker.compose.service"]
        container_checks = {
            "healthy": item["State"].get("Health", {}).get("Status") == "healthy",
            "image_locked": item["Image"] in role_images,
            "unprivileged": item["Config"]["User"] == "1000:1000"
            and not item["HostConfig"]["Privileged"],
            "root_readonly": item["HostConfig"]["ReadonlyRootfs"],
            "all_caps_dropped": item["HostConfig"]["CapDrop"] == ["ALL"],
            "loopback_host_network": item["HostConfig"]["NetworkMode"] == "host",
            "no_docker_socket_or_host_conda": all(
                "docker.sock" not in row["Source"]
                and not row["Source"].startswith("/home/h3c/anaconda3")
                for row in item["Mounts"]
            ),
            "no_private_value_in_image_environment": not any(
                value in json.dumps(image["Config"]).encode() for value in credentials
            ),
            "synthetic_disabled": "SCAT_S5_SYNTHETIC=0" in item["Config"]["Env"],
        }
        checks[service + "_configuration"] = all(container_checks.values())
        rows.append(
            {
                "service": service,
                "id": item["Id"][:12],
                "image_id": item["Image"],
                "checks": container_checks,
                "memory_bytes": item["HostConfig"]["Memory"],
                "cpus": item["HostConfig"]["NanoCpus"] / 1e9,
                "mounts": [
                    {"source": row["Source"], "target": row["Destination"], "writable": row["RW"]}
                    for row in item["Mounts"]
                ],
            }
        )
    checks["seven_owned_containers"] = len(rows) == 7
    checks["actual_private_value_scan_clean"] = not exposures
    source = read(DEPLOY / "context/source.lock.json")
    checks["built_source_snapshot_intact"] = all(
        sha(DEPLOY / "context/app" / name) == value for name, value in source.items()
    )
    checks["built_runtime_matches_current_project"] = all(
        sha(ROOT / name) == value
        for name, value in source.items()
        if name.startswith("src/")
        or name in {"scripts/run_container_service.py", "scripts/run_hermes_api.py"}
    )
    state = DEPLOY / "shared/state/tasks.sqlite"
    with sqlite3.connect(state) as db:
        db.row_factory = sqlite3.Row
        accounts = [
            dict(row)
            for row in db.execute(
                "SELECT scope,run_id,llm_calls,charged_tokens,provider_input_tokens,provider_output_tokens FROM skill_run_budgets"
            )
        ]
        checks["one_worker_claim_one_native_child"] = (
            db.execute("SELECT count(*) FROM skill_job_events WHERE kind='claimed'").fetchone()[0]
            == 1
            and db.execute(
                "SELECT count(*) FROM skill_job_events WHERE kind='native_run_bound'"
            ).fetchone()[0]
            == 1
        )
    # Each job account already imports its planner once. Preserve the failed
    # first planner separately, rather than double-counting the successful one.
    first = read(OUT / "workflow-result.json")
    calls = sum(row["llm_calls"] for row in accounts) + sum(
        row["llm_calls"] for row in first["main_budgets"]
    )
    charged = sum(row["charged_tokens"] for row in accounts) + sum(
        row["charged_tokens"] for row in first["main_budgets"]
    )
    checks["campaign_budget_including_failure"] = calls <= 72 and charged <= 3000000
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "tests": tests,
        "containers": rows,
        "images": locks,
        "credential_scan": {"files": scanned, "exposure_files": exposures},
        "model_requests_including_failure": calls,
        "conservative_tokens_including_failure": charged,
        "gpu_migration": "pending; existing RAG and BentoML retained",
        "new_host_environments": 0,
        "host_dependency_installs": 0,
    }
    (ROOT / "docs/validation/20261027-h7b-acceptance.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "containers"}))
    assert all(checks.values()), "deployment_audit_failed"


if __name__ == "__main__":
    main()
