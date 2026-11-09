"""Generate the versioned single-host Compose topology without any credential values."""

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = "/home/h3c/project/python/scat-hermes-platform"
DEPLOY = APP + "/var/deploy/h7b"


def mount(source, target, readonly=False):
    return {
        "type": "bind",
        "source": source,
        "target": target,
        "read_only": readonly,
        "bind": {"create_host_path": False},
    }


def main():
    common = {
        "init": True,
        "network_mode": "host",
        "user": "1000:1000",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "stop_grace_period": "40s",
        "pids_limit": 256,
        "cpus": 1.0,
        "mem_limit": "1536m",
        "logging": {"driver": "json-file", "options": {"max-size": "4m", "max-file": "3"}},
        "environment": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "HERMES_API_KEY_FILE": "var/deploy/h7b/secrets/hermes.key",
            "HERMES_BASE_URL": "http://127.0.0.1:18121",
            "HERMES_STATE_FILE": "var/deploy/h7b/shared/state/bindings.sqlite",
            "HERMES_TASK_STATE_FILE": "var/deploy/h7b/shared/state/tasks.sqlite",
            "HERMES_RESULT_STATE_FILE": "var/deploy/h7b/shared/state/results.sqlite",
            "SCAT_SKILL_RELEASE_STATE": "var/deploy/h7b/shared/skills/catalog.sqlite",
            "SCAT_S5_WORKFLOWS": "1",
            "SCAT_SKILL_JOBS": "1",
            "SCAT_SKILL_JOB_STATE": DEPLOY + "/shared/state/tasks.sqlite",
            "ANALYSIS_SINGLE_USER_ID": "scat-researcher",
            "ANALYSIS_IDENTITY_MODE": "single_user",
            "SCAT_RAG_BASELINE_ENABLED": "true",
            "SCAT_RAG_BASELINE_URL": "http://127.0.0.1:18133",
            "SCAT_GBAR_V23_ENABLED": "false",
            "SCAT_PARENT_BUDGET_PATH": DEPLOY + "/shared/homes/main/skill-loads.sqlite",
            "SCAT_S5_SYNTHETIC": "0",
        },
        "volumes": [
            mount(DEPLOY + "/shared", DEPLOY + "/shared"),
            mount(DEPLOY + "/runtime-validation", APP + "/var/validation"),
            mount(DEPLOY + "/workflow-inputs", APP + "/var/workflow-inputs"),
            mount(DEPLOY + "/rag-operations", APP + "/var/rag-operations"),
            mount(APP + "/var/rag/corpora", APP + "/var/rag/corpora", True),
        ],
    }
    definitions = {
        "actions": ("business", "actions", 18123, "2g", 2),
        "platform": ("business", "platform", 18120, "1536m", 1),
        "hermes-main": ("native", "main", 18121, "2g", 2),
        "hermes-worker": ("native", "job-control", 18122, "2g", 1),
        "worker": ("business", "worker", 18126, "1g", 1),
        "author-api": ("author", "author-api", 18124, "3g", 2),
        "hermes-author": ("native", "author", 18125, "1536m", 1),
    }
    keys = {
        "platform": ["hermes", "rpc-platform"],
        "actions": ["rpc-main", "rpc-worker", "rpc-platform"],
        "hermes-main": ["hermes", "rpc-main"],
        "worker": ["hermes", "rpc-worker", "job-control"],
        "hermes-worker": ["hermes", "rpc-worker", "job-control"],
        "author-api": ["author", "release", "hermes"],
        "hermes-author": ["hermes", "author"],
    }
    services = {}
    for name, (image, role, port, memory, cpus) in definitions.items():
        item = deepcopy(common)
        item.update(
            image="${SCAT_" + image.upper() + "_IMAGE}", command=[role], mem_limit=memory, cpus=cpus
        )
        item["volumes"].append(mount(DEPLOY + "/runtime-tmp/" + name, APP + "/.tmp"))
        for key in keys[name]:
            item["volumes"].append(
                mount(
                    DEPLOY + "/secrets/" + key + ".key", DEPLOY + "/secrets/" + key + ".key", True
                )
            )
        if image in {"business", "author"}:
            item["env_file"] = [DEPLOY + "/secrets/backend.env"]
            if name == "platform":
                item["env_file"].append(DEPLOY + "/secrets/platform.env")
        if image in {"native", "author"}:
            item["environment"]["SCAT_HERMES_PROFILE_ROOT"] = "var/deploy/h7b/profile-roots/" + name
            item["volumes"].append(
                mount(DEPLOY + "/profile-roots/" + name, DEPLOY + "/profile-roots/" + name)
            )
        if name in {"platform", "worker", "hermes-main", "hermes-worker"}:
            item["environment"].update(
                SCAT_ACTION_BASE_URL="http://127.0.0.1:18123",
                SCAT_ACTION_KEY_FILE="var/deploy/h7b/secrets/rpc-"
                + (
                    "main"
                    if name == "hermes-main"
                    else "platform"
                    if name == "platform"
                    else "worker"
                )
                + ".key",
            )
        if name in {"platform", "worker"}:
            item["environment"]["SCAT_EXECUTION_REMOTE"] = "1"
        if name == "worker":
            item["environment"]["SCAT_JOB_CONTROL_URL"] = "http://127.0.0.1:18122"
        if name == "hermes-main":
            item["environment"]["SCAT_MCP_URL"] = "http://127.0.0.1:18123/mcp/"
        if name in {"author-api", "hermes-author"}:
            item["profiles"] = ["author"]
            item["environment"]["SCAT_AUTHOR_KEY_FILE"] = "var/deploy/h7b/secrets/author.key"
        if name == "author-api":
            item["environment"]["SCAT_AUTHOR_RELEASE_KEY_FILE"] = (
                "var/deploy/h7b/secrets/release.key"
            )
            item["volumes"].append(mount(DEPLOY + "/secrets/author.env", APP + "/.env", True))
        path = "/health/ready" if name == "platform" else "/health"
        item["healthcheck"] = {
            "test": [
                "CMD",
                "/usr/local/bin/scat-python",
                "-B",
                "-c",
                "import urllib.request;urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:"
                + str(port)
                + path
                + "',timeout=4)",
            ],
            "interval": "15s",
            "timeout": "6s",
            "start_period": "90s",
            "retries": 3,
        }
        deps = {
            "hermes-main": ["actions"],
            "platform": ["hermes-main"],
            "worker": ["actions", "platform", "hermes-worker"],
            "hermes-author": ["author-api"],
        }.get(name, [])
        if deps:
            item["depends_on"] = {dep: {"condition": "service_healthy"} for dep in deps}
        services[name] = item
    output = {
        "name": "scat-hermes-h7b",
        "services": services,
        "x-external-gpu-services": {
            "rag": {
                "endpoint": "http://127.0.0.1:18133",
                "mode": "existing-conda-service",
                "candidate_port": 18134,
            },
            "gbar": {
                "mode": "existing-bentoml-container",
                "container": "scat-gbar-production-gbar-1",
                "candidate_port": 18127,
            },
            "migration": "GPU_MIGRATION.md; no unbuilt image or unverified GPU service is enabled",
        },
    }
    (ROOT / "infra/compose/compose.yaml").write_text(json.dumps(output, indent=2) + "\n")
    overlay = {
        "services": {
            name: {"environment": {"SCAT_S5_SYNTHETIC": "1"}}
            for name in ["actions", "platform", "worker"]
        }
    }
    overlay["services"]["hermes-main"] = {
        "environment": {
            "SCAT_READONLY_BUDGET_JSON": json.dumps(
                {
                    "max_turns": 24,
                    "max_llm_tokens": 600000,
                    "max_tool_calls": 60,
                    "wall_seconds": 360,
                }
            )
        }
    }
    (ROOT / "infra/compose/compose.smoke.yaml").write_text(json.dumps(overlay, indent=2) + "\n")
    print(
        json.dumps(
            {"services": list(services), "default_cpu": 5, "optional_author": 2, "gated_gpu": 2}
        )
    )


if __name__ == "__main__":
    main()
