"""Attest the final release, retire QA caps and clean only this round's verified scratch paths."""

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=45)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    os.umask(0o077)
    target = OUT / "final-closeout.json"
    assert not target.exists(), "already_closed_inspect_record"
    assert not Path("/var/lib/scat-hermes-portal/maintenance").exists(), (
        "public_gateway_must_be_open"
    )
    assert (
        hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest()
        == "cd9ec4720e31c8e85402c69b0d0e84131f41a6b1eb62a54dddf9bf35eadfdcb0"
    )
    assert "1525 passed, 2 skipped" in (OUT / "backend-full-scope-final-r2.log").read_text()
    health = json.loads((OUT / "health-postswitch-5m.json").read_text())
    assert not health["failures"] and health["rounds"] == 30 and len(health["services"]) == 10
    assert (
        json.loads((OUT / "natural-materialization-report-r2.json").read_text())["status"]
        == "passed_with_documented_scope"
    )
    assert json.loads((OUT / "mixed-load.json").read_text())["status"] == "passed"
    active = ROOT / "var/deploy/autonomy/active-release"
    release = active.read_text().strip()
    assert release == "20261108-readiness-4eabaed6f3cd"
    plan = json.loads((OUT / "scope-fix/deployment-plan.json").read_text())
    sources = plan["source_hashes"]
    code = (
        "import hashlib,json;from pathlib import Path;print(json.dumps({p:hashlib.sha256((Path.cwd()/p).read_bytes()).hexdigest() for p in "
        + repr(list(sources))
        + "}))"
    )
    consumers = {}
    for name in plan["services"]:
        container = "scat-hermes-h7b-" + name + "-1"
        info = json.loads(
            run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{"image":{{json .Image}},"health":{{json .State.Health.Status}}}',
                    container,
                ]
            )
        )
        assert info["health"] == "healthy"
        actual = json.loads(
            run(["docker", "exec", container, "/usr/local/bin/scat-python", "-B", "-c", code])
        )
        assert actual == sources
        info["checked_source_files"] = len(sources)
        consumers[name] = info
    gpu = json.loads(
        run(
            [
                "docker",
                "inspect",
                "--format",
                '{"image":{{json .Image}},"health":{{json .State.Health.Status}},"mounts":{{json .Mounts}},"entrypoint":{{json .Config.Entrypoint}}}',
                "scat-gbar-conda-production-1",
            ]
        )
    )
    assert gpu["health"] == "healthy" and gpu["image"].startswith("sha256:8607065d54ce")
    assert gpu["entrypoint"][-1] == "/usr/local/libexec/scat_gpu_driver_guard.py"
    bindings = [m for m in gpu["mounts"] if m["Type"] == "bind"]
    assert all(not m["RW"] for m in bindings)
    assert {m["Destination"] for m in bindings} == {
        "/opt/bento/src/artifacts",
        "/etc/cdi/scat-nvidia.json",
    }
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM gpu_requests WHERE state IN ('running','unknown')"
            ).fetchone()[0]
            == 0
        )
        production_gpu = db.execute("SELECT count(*),sum(calls) FROM gpu_requests").fetchone()
    assert production_gpu == (11, 11)
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/readiness-conda-requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        candidate_gpu = db.execute(
            "SELECT count(*),sum(calls) FROM gpu_requests WHERE state='succeeded'"
        ).fetchone()
    assert candidate_gpu == (3, 3)
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/validation/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        totals = db.execute(
            "SELECT campaign,count(*),sum(units) FROM validation_calls GROUP BY campaign ORDER BY campaign"
        ).fetchall()
    assert totals == [
        ("release-readiness-20261109-A", 96, 6079891),
        ("release-readiness-20261109-B", 48, 2809602),
    ]
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        assert (
            db.execute("SELECT count(*) FROM model_calls WHERE state='reserved'").fetchone()[0] == 0
        )
    policy = ROOT / "var/deploy/validation-campaign/active.json"
    assert json.loads(policy.read_text())["id"] == "release-readiness-20261109-B"
    pending = policy.with_suffix(".pending")
    save(pending, {"schema_version": 1, "status": "retired"})
    pending.chmod(0o600)
    pending.replace(policy)
    save(
        OUT / "campaign-closeout.json",
        {
            "status": "retired",
            "batches": totals,
            "total_calls": 144,
            "total_conservative_units": 8889493,
            "failed_unknown_refunded": False,
            "ordinary_per_run_and_relay_caps_unchanged": True,
            "closed_at": time.time(),
        },
    )
    cleanup = []
    review = json.loads((OUT / "legacy-review-process.json").read_text())
    process = Path("/proc") / str(review["pid"])
    if process.exists():
        assert process.stat().st_uid == os.getuid()
        assert b"scripts/serve_readiness_legacy_review.py" in (process / "cmdline").read_bytes()
        os.kill(review["pid"], signal.SIGTERM)
        for _ in range(30):
            if not process.exists():
                break
            time.sleep(0.1)
        cleanup.append("private_renderer_harness_stopped")
    stopped = run(["docker", "ps", "-a", "--format", "{{.Names}}"]).splitlines()
    names = [
        n
        for n in stopped
        if n
        in {
            "scat-readiness-smoke-actions-1",
            "scat-readiness-smoke-hermes-main-1",
            "scat-readiness-smoke-platform-1",
            "scat-gbar-conda-readiness-1",
        }
    ]
    for name in names:
        assert run(["docker", "inspect", "--format={{.State.Running}}", name]).strip() == "false"
        run(["docker", "rm", name])
    cleanup.append({"owned_stopped_test_containers_removed": names})
    temporary = ROOT / ".tmp/release-readiness"
    assert (
        temporary.resolve()
        == Path("/home/h3c/project/python/scat-hermes-platform/.tmp/release-readiness")
        and not temporary.is_symlink()
    )
    running_mounts = json.loads(run(["docker", "inspect", *run(["docker", "ps", "-q"]).split()]))
    assert not any(
        str(m["Source"]).startswith(str(temporary)) for c in running_mounts for m in c["Mounts"]
    )
    if temporary.exists():
        shutil.rmtree(temporary)
    cleanup.append("verified_round_temporary_directory_removed")
    failed_test_temp = Path("/tmp/pytest-of-h3c/pytest-47")
    if failed_test_temp.exists():
        assert (
            failed_test_temp.resolve() == failed_test_temp
            and not failed_test_temp.is_symlink()
            and failed_test_temp.stat().st_uid == os.getuid()
        )
        shutil.rmtree(failed_test_temp)
        cleanup.append("exact_owned_failed_pytest_47_removed")
    previous = json.loads((OUT / "qa-scheduler-pause.json").read_text())["previous"]
    if previous == "active":
        subprocess.run(
            ["systemctl", "--user", "start", "scat-auto-skill-learning.service"],
            check=True,
            timeout=45,
        )
    scheduler = subprocess.run(
        ["systemctl", "--user", "is-active", "scat-auto-skill-learning.service"],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    ).stdout.strip()
    assert scheduler == previous
    result = {
        "status": "closed",
        "cpu_release": release,
        "gpu_release": "scat-gbar-conda:20261109-8607065d54ce",
        "server_rebooted": False,
        "public_gateway_open": True,
        "consumers": consumers,
        "gpu_readonly_mounts_and_guard": True,
        "default_runtime": run(["docker", "info", "--format={{.DefaultRuntime}}"]).strip(),
        "production_gpu_receipts": production_gpu[0],
        "new_gpu_calls_this_round": 5,
        "qa_calls": 144,
        "qa_conservative_units": 8889493,
        "qa_campaign_retired": True,
        "learning_scheduler_restored": scheduler,
        "backend_passed": 1525,
        "backend_skipped": 2,
        "post_switch_health_seconds": health["duration_seconds"],
        "post_switch_health_failures": 0,
        "disk_free_gib": round(shutil.disk_usage(ROOT).free / 1024**3, 2),
        "cleanup": cleanup,
        "human_rag_science_distribution_approval": "pending",
        "full_legacy_business_parity": "not_claimed",
        "raw_business_payloads_exported": False,
    }
    save(target, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
