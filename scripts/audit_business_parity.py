"""Read only deployment, receipt and budget metadata; never load service credentials."""

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-business-parity"


def command(args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def read(path):
    return json.loads(path.read_text())


def main():
    plan = read(OUT / "deployment-plan.json")
    report = {"containers": {}}
    for name, previous in plan["before_ids"].items():
        value = json.loads(
            command(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{"id":{{json .Id}},"image":{{json .Image}},"health":{{json .State.Health.Status}}}',
                    "scat-hermes-h7b-" + name + "-1",
                ]
            )
        )
        assert value["health"] == "healthy"
        value["unchanged_id"] = value["id"] == previous
        if name not in plan["services"]:
            assert value["unchanged_id"]
        else:
            assert value["image"] == plan["image"]
        report["containers"][name] = value
    report["server_rebooted"] = (
        hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
        != plan["boot_sha256"]
    )
    assert not report["server_rebooted"]
    report["backend_release"] = (ROOT / "var/deploy/autonomy/active-release").read_text().strip()
    report["portal_release"] = (ROOT / "var/deploy/portal/active-release").read_text().strip()
    report["units"] = {
        name: command(["systemctl", "--user", "is-active", name])
        for name in [
            "scat-hermes-portal.service",
            "scat-memory-model-proxy.service",
            "scat-auto-skill-learning.service",
        ]
    }
    report["source_hashes"] = {}
    for path, expected in plan["source_hashes"].items():
        value = command(
            ["docker", "exec", "scat-hermes-h7b-platform-1", "sha256sum", str(ROOT / path)]
        ).split()[0]
        assert value == expected
        report["source_hashes"][path] = value
    backups = list((OUT / "private-before-switch").rglob("*.sqlite"))
    for path in backups:
        assert path.stat().st_mode & 0o077 == 0
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    report["private_backups_verified"] = len(backups)
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["autonomy_budget"] = dict(
            zip(
                ["calls", "reserved_units"],
                db.execute("SELECT count(*),sum(reserved) FROM model_calls").fetchone(),
                strict=True,
            )
        )
    with sqlite3.connect(
        "file:" + str(ROOT / "var/deploy/h7b/shared/skills/catalog.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["active_skills"] = db.execute(
            "SELECT sha,generation FROM active WHERE id=1"
        ).fetchone()
    previous = read(ROOT / "var/validation/20261108-platform-closeout/audit-after.json")
    report["gpu_container_unchanged"] = (
        command(["docker", "inspect", "--format", "{{.Id}}", "scat-gbar-production-gbar-1"])
        == previous["gpu_container"]["id"]
    )
    assert report["gpu_container_unchanged"]
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["gpu_receipt_states"] = db.execute(
            "SELECT state,count(*) FROM gpu_requests GROUP BY state"
        ).fetchall()
        report["gpu_receipt_count"] = db.execute("SELECT count(*) FROM gpu_requests").fetchone()[0]
    assert report["gpu_receipt_count"] == len(previous["gpu_receipts"])
    report["new_gpu_calls"] = 0
    natural = read(OUT / "natural.json")
    report["natural_questions"] = len(natural["cases"])
    report["natural_llm_calls"] = sum(
        c.get("budget", {}).get("llm_calls", 0) for c in natural["cases"]
    )
    report["natural_conservative_units"] = sum(
        c.get("budget", {}).get("charged_tokens", 0) for c in natural["cases"]
    )
    report["natural_admission_status"] = natural["status"]
    report["docker_runtimes"] = list(
        json.loads(command(["docker", "info", "--format", "{{json .Runtimes}}"]))
    )
    report["docker_resource_snapshot"] = [
        json.loads(line)
        for line in command(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                "scat-hermes-h7b-platform-1",
                "scat-gbar-production-gbar-1",
            ]
        ).splitlines()
    ]
    report["status"] = "passed"
    (OUT / "final-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"containers", "source_hashes", "docker_resource_snapshot"}
            }
        )
    )


if __name__ == "__main__":
    main()
