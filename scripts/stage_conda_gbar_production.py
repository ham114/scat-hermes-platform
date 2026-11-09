"""Stage guarded CDI/Conda GPU and a CPU registration release; keep rollback unchanged."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import deploy_release_readiness as deploy

ROOT = deploy.ROOT
OUT = ROOT / "var/validation/20261108-release-readiness"


def main():
    os.umask(0o077)
    assert not (OUT / "gpu-conda-production-plan.json").exists()
    assert json.loads((OUT / "gpu-conda-acceptance.json").read_text())["status"] == "passed"
    candidate = json.loads((OUT / "gpu-conda-candidate.json").read_text())
    old_release = (deploy.DEPLOY / "active-release").read_text().strip()
    assert old_release == "20261108-readiness-ca32ef734bbf"
    previous = deploy.DEPLOY / "releases" / old_release
    context = ROOT / "var/deploy/gpu/conda-migration/guarded"
    context.mkdir(exist_ok=False)
    shutil.copyfile(ROOT / "scripts/gpu_driver_guard.py", context / "driver_guard.py")
    tag = "scat-gbar-conda:guarded-" + hashlib.sha256((context / "driver_guard.py").read_bytes()).hexdigest()[:12]
    base = "scat-gbar-conda:readiness-5a0dd6b6286d"
    assert deploy.run(["docker", "image", "inspect", "--format={{.Id}}", base]).strip() == candidate["image"]
    (context / "Dockerfile").write_text("FROM " + base + "\nCOPY --chown=1000:1000 driver_guard.py /usr/local/libexec/scat_gpu_driver_guard.py\nENTRYPOINT [\"/opt/conda/envs/scat-gbar/bin/python\",\"-B\",\"/usr/local/libexec/scat_gpu_driver_guard.py\"]\n")
    with (OUT / "gpu-guarded-build.log").open("x") as log:
        subprocess.run(["docker", "build", "--network=none", "--pull=false", "-t", tag, str(context)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    image = deploy.run(["docker", "image", "inspect", "--format={{.Id}}", tag]).strip()
    release_id = "scat-gbar-conda:20261109-" + image.split(":")[1][:12]
    cpu_release = "20261109-gpu-conda-" + image.split(":")[1][:12]
    folder = deploy.DEPLOY / "releases" / cpu_release
    folder.mkdir(exist_ok=False)
    for filename in ["compose.yaml", "images.lock.json", "source.lock.json", "features.json"]:
        if (previous / filename).exists():
            shutil.copyfile(previous / filename, folder / filename)
    shutil.copyfile(previous / "compose.yaml", folder / "rollback-compose.json")
    old_gpu = json.loads((ROOT / "var/gpu/production.json").read_text())
    new_gpu = {**old_gpu, "transport": {**old_gpu["transport"], "endpoint": "http://172.30.67.4:3000", "release_id": release_id}}
    relative = "var/gpu/conda-production-" + image.split(":")[1][:12] + ".json"
    deploy.save(ROOT / relative, new_gpu)
    config = json.loads((folder / "compose.yaml").read_text())
    changed = []
    for name, service in config["services"].items():
        if "SCAT_GPU_CONFIG_FILE" not in service["environment"]:
            continue
        changed.append(name)
        service["environment"]["SCAT_GPU_CONFIG_FILE"] = relative
        service["volumes"] = [v for v in service["volumes"] if v["target"] != str(ROOT / "var/gpu/production.json")]
        deploy.bind(service, relative, True)
    assert set(changed) == {"actions", "platform", "worker", "hermes-main", "hermes-worker"}
    deploy.save(folder / "compose.yaml", config)
    deploy.run(deploy.compose(folder / "compose.yaml") + ["config", "--quiet"])
    intent = json.loads((OUT / "gpu-conda-candidate-intent.json").read_text())
    args = intent["command"]
    env = dict(a[len("--env="):].split("=", 1) for a in args if a.startswith("--env="))
    env["GBAR_RELEASE_ID"] = release_id
    artifacts = ROOT / "var/deploy/gpu/conda-migration/artifacts"
    service = {"image": image, "container_name": "scat-gbar-conda-production-1", "pull_policy": "never", "restart": "unless-stopped", "read_only": True, "user": "1000:1000", "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 256, "mem_limit": "8g", "cpus": 4, "shm_size": "256m", "tmpfs": ["/tmp:rw,nosuid,size=512m"], "devices": ["nvidia.com/gpu=0"], "networks": {"gbar_internal": {"ipv4_address": "172.30.67.4"}}, "environment": env, "volumes": [{"type": "bind", "source": str(artifacts), "target": "/opt/bento/src/artifacts", "read_only": True, "bind": {"create_host_path": False}}, {"type": "bind", "source": "/etc/cdi/scat-nvidia.json", "target": "/etc/cdi/scat-nvidia.json", "read_only": True, "bind": {"create_host_path": False}}], "healthcheck": {"test": ["CMD", "/opt/conda/envs/scat-gbar/bin/python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:3000/ready',timeout=2)"], "interval": "5s", "timeout": "4s", "start_period": "15s", "retries": 8}, "logging": {"driver": "json-file", "options": {"max-size": "4m", "max-file": "3"}}}
    gpu_compose = context / "compose.json"
    deploy.save(gpu_compose, {"services": {"gbar": service}, "networks": {"gbar_internal": {"external": True, "name": "scat-gbar-production_gbar_internal"}}})
    command = ["docker", "compose", "--project-name", "scat-gbar-conda", "-f", str(gpu_compose)]
    deploy.run(command + ["config", "--quiet"])
    deploy.run(command + ["up", "-d", "--wait", "--wait-timeout", "120"], timeout=150)
    from workflow_runtime.gpu import GPURegistration
    backend = GPURegistration.model_validate(new_gpu).backend()
    try:
        assert backend.health().ready
    finally:
        backend.close()
    backup = OUT / "private/gpu-before-conda"
    backup.mkdir(exist_ok=False)
    shutil.copyfile(ROOT / "var/gpu/production.json", backup / "production.json")
    shutil.copyfile(ROOT / "services/hermes/gpu-models.v1.json", backup / "gpu-models.v1.json")
    plan = {"status": "staged_guarded_service_ready", "previous_cpu_release": old_release, "cpu_release": cpu_release, "services": changed, "gpu_image": image, "gpu_release_id": release_id, "gpu_container": service["container_name"], "gpu_compose": str(gpu_compose.relative_to(ROOT)), "registration_file": relative, "registration_sha256": deploy.sha(ROOT / relative), "boot_sha256": deploy.sha(Path("/proc/sys/kernel/random/boot_id")), "old_gpu_retained": True, "new_model_calls": 0}
    deploy.save(OUT / "gpu-conda-production-plan.json", plan)
    print(json.dumps(plan))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    main()
