"""Read-only final health, receipt, backup, dependency and small HTTP latency inventory."""

import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"


def main():
    before = json.loads((OUT / "audit-before.json").read_text())
    after = json.loads((OUT / "audit-after.json").read_text())
    assert before["boot_sha256"] == after["boot_sha256"]
    assert (
        before["gpu_container"] == after["gpu_container"]
        and before["gpu_metadata"] == after["gpu_metadata"]
    )
    assert all(row["health"] == "healthy" for row in after["containers"].values())
    for name in ["hermes-main", "hermes-worker", "hermes-author"]:
        assert before["containers"][name]["id"] == after["containers"][name]["id"]
    old = {r["id"]: r for r in before["gpu_receipts"]}
    new = {r["id"]: r for r in after["gpu_receipts"]}
    assert len(set(new) - set(old)) == 1 and all(new[k] == v for k, v in old.items())
    private = list(OUT.glob("private-before-management*/*.sqlite")) + list(
        OUT.glob("private-before-management*/**/*.sqlite")
    )
    private = sorted(set(private))
    for p in private:
        assert p.stat().st_mode & 0o077 == 0
        with sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    units = {
        name: subprocess.check_output(["systemctl", "--user", "is-active", name], text=True).strip()
        for name in [
            "scat-auto-skill-learning.service",
            "scat-memory-model-proxy.service",
            "scat-hermes-portal.service",
        ]
    }
    assert all(v == "active" for v in units.values())
    runtimes = json.loads(
        subprocess.check_output(["docker", "info", "--format", "{{json .Runtimes}}"], text=True)
    )
    metadata_code = 'import importlib.metadata as m,json,sys;print(json.dumps({"python":sys.version.split()[0],"packages":[{"name":d.metadata["Name"],"version":d.version,"license_expression":d.metadata.get("License-Expression"),"license_classifiers":[c for c in d.metadata.get_all("Classifier",[]) if c.startswith("License ::")]} for d in m.distributions()]}))'
    dependencies = {}
    for name, env in [("platform", "scat-agent"), ("hermes-main", "scat-hermes")]:
        dependencies[name] = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "scat-hermes-h7b-" + name + "-1",
                    "/opt/conda/envs/" + env + "/bin/python",
                    "-B",
                    "-c",
                    metadata_code,
                ]
            )
        )
    (OUT / "python-dependencies.json").write_text(json.dumps(dependencies, indent=2) + "\n")
    row = json.loads(subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"]))[
        0
    ]
    key = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x)["AUTH_SECRET"]

    async def latency():
        results = {}
        async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
            for name, url, headers in [
                (
                    "core_catalog",
                    "http://127.0.0.1:18120/api/v1/hermes/workflow-catalog",
                    {"Authorization": "Bearer " + key},
                ),
                ("portal_catalog", "http://127.0.0.1:18158/api/v1/hermes/workflow-catalog", {}),
            ]:
                values = []

                async def request(url=url, headers=headers, values=values):
                    start = time.monotonic()
                    response = await http.get(url, headers=headers)
                    assert response.status_code == 200
                    values.append(round((time.monotonic() - start) * 1000, 3))

                for _ in range(5):
                    await asyncio.gather(request(), request())
                ordered = sorted(values)
                results[name] = {
                    "samples": values,
                    "requests": 10,
                    "concurrency": 2,
                    "errors": 0,
                    "p50_ms": ordered[4],
                    "p95_ms": ordered[9],
                }
        return results

    performance = asyncio.run(latency())
    (OUT / "http-latency.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "scope": "20 bounded private HTTP catalog GETs, not GPU throughput or an SLA",
                "results": performance,
            },
            indent=2,
        )
        + "\n"
    )
    report = {
        "status": "passed",
        "release": after["release"],
        "server_not_rebooted": True,
        "gpu_container_unchanged": True,
        "native_containers_unchanged": True,
        "core_health": {k: v["health"] for k, v in after["containers"].items()},
        "units": units,
        "old_gpu_receipts_unchanged": len(old),
        "new_gpu_receipts": 1,
        "private_backup_databases": len(private),
        "backup_quick_check": "ok",
        "backup_permissions": "0600",
        "docker_runtimes": sorted(runtimes),
        "standard_gpu_runtime_migration": "not_performed",
        "cdi_spec_files": [
            str(p)
            for directory in [Path("/etc/cdi"), Path("/var/run/cdi")]
            if directory.exists()
            for p in directory.glob("*")
            if p.suffix in {".yaml", ".json"}
        ],
        "http_get_samples": 20,
        "model_calls_in_final_audit": 0,
    }
    (OUT / "final-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
