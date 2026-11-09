"""Read-only deployment/resource inventory without service credential inspection."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"


def call(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        return {"code": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"code": -1, "error": type(exc).__name__}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "boot_sha256": hashlib.sha256(
            Path("/proc/sys/kernel/random/boot_id").read_bytes()
        ).hexdigest(),
        "backend": (ROOT / "var/deploy/autonomy/active-release").read_text().strip(),
        "portal": (ROOT / "var/deploy/portal/active-release").read_text().strip(),
        "disk_free_bytes": shutil.disk_usage(ROOT).free,
        "kernel_driver": call(["cat", "/proc/driver/nvidia/version"]),
        "host_nvml": call(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader",
            ]
        ),
        "docker_version": call(["docker", "version", "--format", "{{.Server.Version}}"]),
        "toolkit": shutil.which("nvidia-ctk"),
        "cdi_specs": {p: [x.name for x in Path(p).glob("*")] for p in ["/etc/cdi", "/var/run/cdi"]},
        "conda_envs": call(["/home/h3c/anaconda3/bin/conda", "env", "list", "--json"]),
        "containers": {},
    }
    names = [
        "scat-hermes-h7b-" + n + "-1"
        for n in [
            "platform",
            "actions",
            "worker",
            "author-api",
            "hermes-main",
            "hermes-worker",
            "hermes-author",
        ]
    ] + ["scat-gbar-production-gbar-1"]
    for name in names:
        result = call(
            [
                "docker",
                "inspect",
                "--format",
                '{"id":{{json .Id}},"image":{{json .Image}},"health":{{json .State.Health.Status}},"mounts":{{json .Mounts}}}',
                name,
            ]
        )
        report["containers"][name] = json.loads(result["stdout"]) if result["code"] == 0 else result
    report["gbar_cuda"] = call(
        [
            "docker",
            "exec",
            "scat-gbar-production-gbar-1",
            "python",
            "-c",
            "import torch,json;print(json.dumps(dict(cuda=torch.cuda.is_available(),torch=torch.__version__,device=torch.cuda.get_device_name(),memory=torch.cuda.mem_get_info())))",
        ]
    )
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["gpu_receipts"] = db.execute(
            "SELECT id,state,calls,output_sha FROM gpu_requests"
        ).fetchall()
        outputs = db.execute("SELECT output FROM gpu_requests WHERE state='succeeded'").fetchall()
        classifications = []

        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "class_probabilities" and child is not None:
                        classifications.append(
                            {
                                "rows": len(child),
                                "columns": len(child[0])
                                if child and isinstance(child[0], list)
                                else None,
                            }
                        )
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        for (body,) in outputs:
            inspect(json.loads(body))
        report["existing_classification_shapes"] = classifications
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["autonomy_budget"] = db.execute(
            "SELECT count(*),sum(reserved) FROM model_calls"
        ).fetchone()
    target = OUT / "baseline.json"
    assert not target.exists(), "preserve_first_baseline"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"containers", "conda_envs", "gpu_receipts"}
            }
        )
    )


if __name__ == "__main__":
    main()
