"""Start an isolated candidate cluster against private SQLite copies, without model calls."""

import json
import os
import shutil
import sqlite3
import subprocess

import deploy_release_readiness as deploy

ROOT, OUT = deploy.ROOT, deploy.OUT


def main():
    plan = json.loads(deploy.plan_file().read_text())
    release = deploy.DEPLOY / "releases" / plan["release"]
    directory = ROOT / ".tmp/release-readiness" / ("cluster-" + plan["release"])
    directory.mkdir(exist_ok=False)
    shared = directory / "shared"
    shared.mkdir()
    original = ROOT / "var/deploy/h7b/shared"
    for relative in ["state", "skills", "homes"]:
        (shared / relative).mkdir()
    for folder in ["state", "skills"]:
        for path in (original / folder).glob("*.sqlite"):
            with (
                sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as source,
                sqlite3.connect(shared / folder / path.name) as target,
            ):
                source.backup(target)
    sha = plan["new_skill_sha256"]
    shutil.copytree(original / "skills/bundles" / sha, shared / "skills/bundles" / sha)
    with sqlite3.connect(shared / "skills/catalog.sqlite") as db:
        db.execute("UPDATE active SET sha=? WHERE id=1", (sha,))
    c = json.loads((release / "compose.yaml").read_text())
    services = {}
    for name in ["actions", "hermes-main", "platform"]:
        service = c["services"][name]
        service.pop("container_name", None)
        service.pop("depends_on", None)
        service.pop("profiles", None)
        service["restart"] = "no"
        for volume in service["volumes"]:
            if volume["source"] == str(original):
                volume["source"] = str(shared)
        services[name] = service
    services["actions"]["environment"]["SCAT_ACTION_PORT"] = "18132"
    services["actions"]["entrypoint"] = [
        "/opt/conda/envs/scat-agent/bin/python",
        "-B",
        "scripts/run_private_actions.py",
    ]
    services["actions"].pop("command", None)
    main = services["hermes-main"]
    main["environment"].update(
        SCAT_ACTION_BASE_URL="http://127.0.0.1:18132", SCAT_MCP_URL="http://127.0.0.1:18132/mcp/"
    )
    main["entrypoint"] = [
        "/opt/conda/envs/scat-hermes/bin/python",
        "-B",
        "scripts/run_hermes_api.py",
        "--profile",
        "scat-main",
        "--home",
        "var/deploy/h7b/shared/homes/readiness-main-smoke",
        "--port",
        "18131",
        "--with-mcp",
        "--with-skills",
        "--with-model-tasks",
        "--with-workflows",
        "--skills-policy",
        "config/rag-readonly-policy.json",
        "--rag-config",
        "services/rag/real-agent-production-v1.json",
        "--readonly-budget",
        main["environment"].get("SCAT_READONLY_BUDGET_JSON", "{}"),
    ]
    main.pop("command", None)
    platform = services["platform"]
    platform["environment"].update(
        HERMES_BASE_URL="http://127.0.0.1:18131", SCAT_AUTONOMY_CONFIG=""
    )
    platform["entrypoint"] = [
        "/opt/conda/envs/scat-agent/bin/python",
        "-B",
        "scripts/run_platform_api.py",
        "--port",
        "18129",
    ]
    platform.pop("command", None)
    for name, service in services.items():
        port = 18132 if name == "actions" else 18131 if name == "hermes-main" else 18129
        path = "/health/ready" if name == "platform" else "/health"
        service["healthcheck"] = {
            "test": [
                "CMD",
                "/usr/local/bin/scat-python",
                "-B",
                "-c",
                f"import urllib.request;urllib.request.urlopen('http://127.0.0.1:{port}{path}',timeout=2)",
            ],
            "interval": "5s",
            "timeout": "4s",
            "start_period": "15s",
            "retries": 5,
        }
    config = directory / "compose.json"
    config.write_text(json.dumps({"services": services}))
    command = ["docker", "compose", "--project-name", "scat-readiness-smoke", "-f", str(config)]
    try:
        transcript = []
        for name in ["actions", "hermes-main", "platform"]:
            result = subprocess.run(
                command + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "60", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=80,
            )
            transcript.append(result.stdout + result.stderr)
            if result.returncode:
                break
        (OUT / "cluster-smoke-compose.log").write_text("\n".join(transcript))
        report = []
        for name in services:
            identity = "scat-readiness-smoke-" + name + "-1"
            log = subprocess.run(
                ["docker", "logs", "--tail", "100", identity],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            private = OUT / "private"
            private.mkdir(exist_ok=True)
            (private / ("cluster-smoke-" + name + ".log")).write_text(log.stdout + log.stderr)
            report.append({"service": name, "log_tail": (log.stdout + log.stderr)[-1800:]})
        (OUT / "cluster-smoke.json").write_text(
            json.dumps(
                {
                    "status": "passed" if result.returncode == 0 else "failed",
                    "returncode": result.returncode,
                    "new_model_calls": 0,
                    "private_state_copies": True,
                },
                indent=2,
            )
            + "\n"
        )
        print(json.dumps({"returncode": result.returncode, "logs": report}))
    finally:
        subprocess.run(
            command + ["stop", "--timeout", "10"],
            capture_output=True,
            text=True,
            check=False,
            timeout=40,
        )


if __name__ == "__main__":
    os.umask(0o077)
    main()
