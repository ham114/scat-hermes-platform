"""Read-only production GPU preflight; never submit inference or export secrets/data."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
SERVICES = [
    "platform",
    "actions",
    "worker",
    "hermes-main",
    "hermes-worker",
    "author-api",
    "hermes-author",
]


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=45)


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    phase = sys.argv[1] if len(sys.argv) > 1 else "before"
    assert phase in {"before", "after"}
    report = {"phase": phase, "inference_requests": 0, "llm_requests": 0}
    report["release"] = (ROOT / "var/deploy/autonomy/active-release").read_text().strip()
    report["boot_sha256"] = hashlib.sha256(
        Path("/proc/sys/kernel/random/boot_id").read_bytes()
    ).hexdigest()
    report["containers"] = {}
    token = None
    for name in SERVICES:
        row = inspect("scat-hermes-h7b-" + name + "-1")
        env = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x)
        if name == "platform":
            token = env.get("AUTH_SECRET")
        report["containers"][name] = {
            "id": row["Id"],
            "image": row["Image"],
            "health": row["State"].get("Health", {}).get("Status"),
            "flags": {
                k: env[k]
                for k in [
                    "SCAT_GPU_CONFIG_FILE",
                    "SCAT_GBAR_V23_ENABLED",
                    "SCAT_S5_SYNTHETIC",
                    "SCAT_SKILL_RELEASE_STATE",
                    "ANALYSIS_RUNS_COLLECTION",
                    "ANALYSIS_ARTIFACTS_COLLECTION",
                ]
                if k in env
            },
            "gpu_mounts": [
                {k: m[k] for k in ["Source", "Destination", "RW"]}
                for m in row["Mounts"]
                if "/gpu" in m["Destination"]
            ],
        }
    assert token
    req = urllib.request.Request(
        "http://127.0.0.1:18120/api/v1/hermes/workflow-catalog",
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        req, timeout=20
    ) as response:
        report["catalog"] = json.load(response)
    reviewed = json.loads((ROOT / "services/hermes/gpu-models.v1.json").read_text())
    gpu = inspect(reviewed["container_name"])
    assert gpu["Image"] == reviewed["image_id"]
    report["gpu_container"] = {
        "id": gpu["Id"],
        "image": gpu["Image"],
        "health": gpu["State"].get("Health", {}).get("Status"),
    }
    cfg = json.loads((ROOT / "var/gpu/production.json").read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(cfg["transport"]["endpoint"] + "/metadata", timeout=15) as response:
        metadata = json.load(response)
    report["gpu_metadata"] = metadata
    for role in ["platform", "hermes-main", "hermes-worker"]:
        py = (
            "/opt/conda/envs/"
            + ("scat-hermes" if role.startswith("hermes-") else "scat-agent")
            + "/bin/python"
        )
        code = "from pathlib import Path;import json;from hermes_runtime.release_store import ReleaseStore;s=ReleaseStore(Path.cwd());r=s.current();print(json.dumps({'sha':r.bundle_sha256,'versions':{k:v['version'] for k,v in r.entries.items()}}))"
        report["containers"][role]["skills"] = json.loads(
            docker("exec", "scat-hermes-h7b-" + role + "-1", py, "-B", "-c", code)
        )
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["gpu_receipts"] = [
            dict(zip([c[0] for c in cursor.description], r))
            for cursor in [
                db.execute(
                    "SELECT id,task,execution,state,calls,request_sha,output_sha FROM gpu_requests ORDER BY created"
                )
            ]
            for r in cursor
        ]
    report["status"] = "inspected"
    (OUT / ("audit-" + phase + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "release": report["release"],
                "health": {k: v["health"] for k, v in report["containers"].items()},
                "catalog": report["catalog"],
                "gpu_metadata": metadata,
                "gpu_receipts": report["gpu_receipts"],
            }
        )
    )


if __name__ == "__main__":
    main()
