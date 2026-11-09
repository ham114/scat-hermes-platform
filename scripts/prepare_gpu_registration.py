"""Register the existing reviewed CUDA service, without restarting it or changing host drivers."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from hermes_runtime.skills import checked_path
    from workflow_runtime.gpu import GPURegistration

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--output", default="var/gpu/production.json")
    args = parser.parse_args()
    reviewed = json.loads((ROOT / "services/hermes/gpu-models.v1.json").read_text())
    value = subprocess.run(
        ["docker", "inspect", reviewed["container_name"]],
        capture_output=True,
        text=True,
        check=True,
    )
    container = json.loads(value.stdout)[0]
    if (
        container["Image"] != reviewed["image_id"]
        or container["State"].get("Health", {}).get("Status") != "healthy"
    ):
        raise RuntimeError("Reviewed GPU container is unavailable or image changed")
    addresses = [
        n["IPAddress"] for n in container["NetworkSettings"]["Networks"].values() if n["IPAddress"]
    ]
    if len(addresses) != 1:
        raise RuntimeError("Ambiguous GPU private service address")
    config = GPURegistration.model_validate(
        {
            "enabled": True,
            "transport": {
                "endpoint": "http://" + addresses[0] + ":3000",
                "release_id": reviewed["release_id"],
                "max_connect_retries": 0,
            },
            "provenance": reviewed["provenance"],
        }
    )
    backend = config.backend()
    try:
        health = backend.health()
        if not health.ready:
            raise RuntimeError("Pinned CUDA service metadata verification failed")
    finally:
        backend.close()
    path = checked_path(ROOT, args.output)
    if not path.is_relative_to(ROOT / "var/gpu"):
        raise RuntimeError("Registration output must be in project var/gpu")
    if args.enable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        body = config.model_dump_json(indent=2) + "\n"
        if path.exists():
            if GPURegistration.model_validate_json(path.read_text()) != config:
                raise RuntimeError("Existing GPU registration differs; no overwrite")
        else:
            with path.open("x") as stream:
                path.chmod(0o600)
                stream.write(body)
    print(
        json.dumps(
            {
                "status": "registered" if args.enable else "verified_preview",
                "config_file": str(path.relative_to(ROOT)),
                "gpu": config.pin(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
