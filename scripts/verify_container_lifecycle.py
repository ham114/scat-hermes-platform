"""Recreate only owned, idle containers and verify persisted receipts without model replay."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/h7b"
OUT = ROOT / "var/validation/20261027-h7b"


def docker(*args, timeout=90):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def budgets():
    rows = {}
    for name, path in [
        ("main", DEPLOY / "shared/homes/main/skill-loads.sqlite"),
        ("jobs", DEPLOY / "shared/state/tasks.sqlite"),
    ]:
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            rows[name] = db.execute(
                "SELECT scope,run_id,llm_calls,charged_tokens FROM skill_run_budgets ORDER BY scope,run_id"
            ).fetchall()
    return rows


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    import httpx

    os.umask(0o077)
    workflow_dir = OUT / "workflow-network-v2"
    workflow = json.loads((workflow_dir / "workflow-result.json").read_text())
    if workflow["status"] != "passed":
        raise ValueError("workflow_must_be_verified_before_recreation")
    state = DEPLOY / "shared/state/tasks.sqlite"
    with sqlite3.connect(state) as db:
        if db.execute(
            "SELECT 1 FROM skill_jobs WHERE state IN ('queued','running','cancel_requested')"
        ).fetchone():
            raise ValueError("owned_jobs_not_idle")
        before_executions = db.execute(
            "SELECT id,state,calls,output_sha FROM skill_backend_executions ORDER BY id"
        ).fetchall()
    auth = {"Authorization": "Bearer " + (DEPLOY / "secrets/platform-auth.key").read_text().strip()}
    origin = "http://127.0.0.1:18120/api/v1/hermes"
    endpoint = origin + "/threads/" + workflow["thread_id"] + "/model-tasks/" + workflow["task_id"]
    before_budget = budgets()
    checks = {}
    with httpx.Client(trust_env=False, timeout=30) as http:
        before = http.get(endpoint, headers=auth)
        before.raise_for_status()
        before = before.json()
        before_ids = docker(
            "ps", "-q", "--filter", "label=com.docker.compose.project=scat-hermes-h7b"
        ).splitlines()
        for action in ["down", "up"]:
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "scripts/manage_container_deployment.py",
                    action,
                    "--author",
                ],
                cwd=ROOT,
                check=True,
                timeout=300,
            )
        after_ids = docker(
            "ps", "-q", "--filter", "label=com.docker.compose.project=scat-hermes-h7b"
        ).splitlines()
        checks["seven_containers_recreated"] = len(after_ids) == 7 and not set(before_ids) & set(
            after_ids
        )
        after = http.get(endpoint, headers=auth)
        after.raise_for_status()
        checks["completed_task_persisted"] = after.json()["skill_job"]["state"] == "succeeded"
        checks["artifact_ids_persisted"] = (
            after.json()["run"]["result_artifact_ids"] == before["run"]["result_artifact_ids"]
        )
        response = http.get(endpoint + "/artifacts/" + workflow["result_artifact_id"], headers=auth)
        response.raise_for_status()
        old = json.loads((workflow_dir / "workflow-artifact.json").read_text())
        checks["artifact_payload_persisted"] = (
            response.json()["payload_sha256"] == old["payload_sha256"]
        )
        other = http.post(origin + "/threads", headers=auth, json={})
        other.raise_for_status()
        checks["cross_thread_404_after_recreation"] = (
            http.get(
                endpoint.replace(workflow["thread_id"], other.json()["thread_id"]), headers=auth
            ).status_code
            == 404
        )
        checks["synthetic_mode_removed"] = (
            http.get(origin + "/workflow-catalog", headers=auth).json()["synthetic"] is False
        )
    # An unavailable execution owner must produce unknown, never a false death
    # confirmation or an automatic replay. The probe has no business operation.
    target = "scat-hermes-h7b-actions-1"
    docker("pause", target)
    try:
        code = "import os,json;from pathlib import Path;from deployment_runtime.client import action_request;r=action_request(Path.cwd(),os.environ,'processes',{'action':'birth','pid':2147483647},timeout=2);print(json.dumps(r));assert r=={'status':'unknown','automatic_replay':False}"
        result = docker(
            "exec",
            "scat-hermes-h7b-worker-1",
            "/usr/local/bin/scat-python",
            "-B",
            "-c",
            code,
            timeout=15,
        )
        checks["owner_unavailable_unknown_no_replay"] = json.loads(result)["status"] == "unknown"
    finally:
        docker("unpause", target)
    with httpx.Client(trust_env=False, timeout=10) as http:
        checks["owner_recovered"] = http.get("http://127.0.0.1:18123/health").status_code == 200
    with sqlite3.connect(state) as db:
        after_executions = db.execute(
            "SELECT id,state,calls,output_sha FROM skill_backend_executions ORDER BY id"
        ).fetchall()
    checks["no_backend_reexecution"] = before_executions == after_executions
    checks["no_paid_model_replay"] = before_budget == budgets()
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "budgets_before": before_budget,
        "budgets_after": budgets(),
        "executions": after_executions,
    }
    (OUT / "lifecycle.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    assert all(checks.values()), "container_lifecycle_failed"


if __name__ == "__main__":
    main()
