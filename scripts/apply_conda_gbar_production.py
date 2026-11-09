"""Switch five idle consumers to guarded GPU registration; keep exact rollback files."""
import json
import os
import shutil
import sqlite3
from pathlib import Path

import deploy_forecast_contract as prior
import deploy_release_readiness as deploy

ROOT = deploy.ROOT
OUT = ROOT / "var/validation/20261108-release-readiness"


def main():
    os.umask(0o077)
    plan = json.loads((OUT / "gpu-conda-production-plan.json").read_text())
    target = OUT / "gpu-conda-production-result.json"
    assert not target.exists()
    assert Path("/var/lib/scat-hermes-portal/maintenance").exists()
    assert deploy.sha(Path("/proc/sys/kernel/random/boot_id")) == plan["boot_sha256"]
    assert (deploy.DEPLOY / "active-release").read_text().strip() == plan["previous_cpu_release"]
    assert deploy.sha(ROOT / plan["registration_file"]) == plan["registration_sha256"]
    prior.idle()
    with sqlite3.connect("file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM gpu_requests WHERE state IN ('running','unknown')").fetchone()[0] == 0
        before = db.execute("SELECT count(*),sum(calls) FROM gpu_requests").fetchone()
    folder = deploy.DEPLOY / "releases" / plan["cpu_release"]
    backup = OUT / "private/gpu-before-conda"
    for relative in ["var/gpu/requests.sqlite", "var/deploy/h7b/shared/state/tasks.sqlite"]:
        destination = backup / Path(relative).name
        if destination.exists():
            with sqlite3.connect("file:" + str(destination) + "?mode=ro", uri=True) as retained:
                assert retained.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            continue
        with sqlite3.connect("file:" + str(ROOT / relative) + "?mode=ro", uri=True) as source, sqlite3.connect(destination) as dest:
            source.backup(dest)
            assert dest.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        destination.chmod(0o600)
    reviewed = ROOT / "services/hermes/gpu-models.v1.json"
    current = ROOT / "var/gpu/production.json"
    try:
        deploy.run(deploy.compose(folder / "rollback-compose.json") + ["stop", "--timeout", "30", *plan["services"]], timeout=180)
        prior.idle()
        for tier in [["actions", "hermes-worker"], ["hermes-main"], ["platform"], ["worker"]]:
            deploy.run(deploy.compose(folder / "compose.yaml") + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", *tier], timeout=160)
        health = {n: deploy.metadata(n)["health"] for n in plan["services"]}
        assert all(v == "healthy" for v in health.values())
        checks = {}
        code = "import json,os,hashlib,urllib.request;from pathlib import Path;p=Path.cwd()/os.environ['SCAT_GPU_CONFIG_FILE'];c=json.loads(p.read_text());o=urllib.request.build_opener(urllib.request.ProxyHandler({}));m=json.load(o.open(c['transport']['endpoint']+'/metadata',timeout=5));assert m['ready'] and m['device']=='cuda' and m['provenance']==c['provenance'] and m['release_id']==c['transport']['release_id'];print(json.dumps({'release':m['release_id'],'endpoint':c['transport']['endpoint'],'registration_file_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
        for name in plan["services"]:
            python = "/opt/conda/envs/" + ("scat-hermes" if name.startswith("hermes-") else "scat-agent") + "/bin/python"
            result = json.loads(deploy.run(["docker", "exec", "scat-hermes-h7b-" + name + "-1", python, "-B", "-c", code]))
            assert result["release"] == plan["gpu_release_id"] and result["endpoint"] == "http://172.30.67.4:3000"
            assert result["registration_file_sha256"] == plan["registration_sha256"]
            checks[name] = result
        pending = current.with_suffix(".pending")
        shutil.copyfile(ROOT / plan["registration_file"], pending)
        pending.chmod(0o600)
        pending.replace(current)
        model = json.loads(reviewed.read_text())
        model.update(container_name=plan["gpu_container"], image_id=plan["gpu_image"], release_id=plan["gpu_release_id"])
        deploy.save(reviewed, model)
        (deploy.DEPLOY / "active-release").write_text(plan["cpu_release"])
        assert deploy.sha(Path("/proc/sys/kernel/random/boot_id")) == plan["boot_sha256"]
        with sqlite3.connect("file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True) as db:
            assert db.execute("SELECT count(*),sum(calls) FROM gpu_requests").fetchone() == before
        deploy.save(target, {"status": "applied_behind_gate", "release": plan["cpu_release"], "gpu_release": plan["gpu_release_id"], "health": health, "consumer_registration_checks": checks, "old_gpu_retained_for_rollback": True, "new_model_calls_during_switch": 0, "server_rebooted": False})
        # Release the tested candidate instance, leaving the immutable image and receipts.
        deploy.run(["docker", "stop", "--time=30", "scat-gbar-conda-readiness-1"])
        print(json.dumps({"status": "applied_behind_gate", "release": plan["cpu_release"], "new_model_calls": 0}))
    except Exception:
        shutil.copyfile(backup / "production.json", current)
        shutil.copyfile(backup / "gpu-models.v1.json", reviewed)
        deploy.run(deploy.compose(folder / "rollback-compose.json") + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", *plan["services"]], timeout=180)
        (deploy.DEPLOY / "active-release").write_text(plan["previous_cpu_release"])
        deploy.save(OUT / "gpu-conda-production-rollback.json", {"status": "rolled_back", "release": plan["previous_cpu_release"], "server_rebooted": False})
        raise


if __name__ == "__main__":
    main()
