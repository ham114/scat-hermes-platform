"""Start CDI/Conda GBAR beside production, with immutable artifacts and a driver guard."""
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
NAME = "scat-gbar-conda-readiness-1"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    os.umask(0o077)
    target = OUT / "gpu-conda-candidate.json"
    assert not target.exists()
    assert "580.173.02" in Path("/proc/driver/nvidia/version").read_text()
    assert sha(Path("/etc/cdi/scat-nvidia.json")) == "2a8752f7140605aac289d71bca0375df8072f18d3b16af05d94866eeed1294b1"
    assert subprocess.run(["docker", "container", "inspect", NAME], capture_output=True, check=False).returncode != 0
    c = ROOT / "var/deploy/gpu/conda-migration/context"
    artifacts = ROOT / "var/deploy/gpu/conda-migration/artifacts"
    for file, row in json.loads((c / "artifacts.lock.json").read_text()).items():
        assert sha(artifacts / file) == row["sha256"]
    for _ in range(240):
        if (OUT / "gpu-conda-image.json").exists():
            break
        time.sleep(5)
    image = json.loads((OUT / "gpu-conda-image.json").read_text())["image"]
    reviewed = json.loads((ROOT / "services/hermes/gpu-models.v1.json").read_text())
    release = "scat-gbar-conda:20261109-" + image.split(":")[1][:12]
    env = {"GBAR_SOURCE_DIR": "/opt/bento/src/artifacts/source", "GBAR_CONFIG": "/opt/bento/src/artifacts/config_v25.json", "GBAR_CHECKPOINT": "/opt/bento/src/artifacts/epoch_70.pt", "GBAR_SOURCE_MANIFEST": "/opt/bento/src/source_manifest.json", "GBAR_SOURCE_TREE_SHA256": reviewed["provenance"]["source_tree_sha256"], "GBAR_CONFIG_SHA256": reviewed["provenance"]["config_sha256"], "GBAR_CHECKPOINT_SHA256": reviewed["provenance"]["checkpoint_sha256"], "GBAR_SOURCE_URI": reviewed["provenance"]["source_uri"], "GBAR_DEVICE": "cuda", "GBAR_TIMEOUT_SECONDS": "120", "GBAR_RELEASE_ID": release}
    args = ["docker", "run", "-d", "--pull=never", "--name=" + NAME, "--network=scat-gbar-production_gbar_internal", "--device=nvidia.com/gpu=0", "--read-only", "--user=1000:1000", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=256", "--memory=8g", "--cpus=4", "--tmpfs=/tmp:rw,nosuid,size=512m", "--shm-size=256m", "--mount=type=bind,src=" + str(artifacts) + ",dst=/opt/bento/src/artifacts,readonly", "--log-opt=max-size=4m", "--log-opt=max-file=3"]
    for key, value in env.items():
        args += ["--env=" + key + "=" + value]
    args += [image]
    intent = {"status": "launch_intent", "image": image, "release_id": release, "name": NAME, "command": args, "new_model_calls": 0}
    (OUT / "gpu-conda-candidate-intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    started = time.monotonic()
    subprocess.run(args, capture_output=True, check=True, timeout=30)
    info = json.loads(subprocess.check_output(["docker", "inspect", NAME]))[0]
    address = info["NetworkSettings"]["Networks"]["scat-gbar-production_gbar_internal"]["IPAddress"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    endpoint = "http://" + address + ":3000"
    for _ in range(180):
        try:
            with opener.open(endpoint + "/ready", timeout=2) as response:
                assert json.load(response)["ready"] is True
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError("candidate_not_ready_production_unchanged")
    with opener.open(endpoint + "/metadata", timeout=3) as response:
        metadata = json.load(response)
    assert metadata["provenance"] == reviewed["provenance"]
    assert metadata["device"] == "cuda" and metadata["model_load_count"] == 1
    result = {"status": "ready", "image": image, "release_id": release, "container": NAME, "endpoint": endpoint, "cold_start_seconds": round(time.monotonic() - started, 3), "metadata": metadata, "standard_cdi": "nvidia.com/gpu=0", "artifacts_readonly": True, "host_environment_mounts": 0, "new_model_calls": 0, "production_switched": False}
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
