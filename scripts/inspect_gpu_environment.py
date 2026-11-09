"""Inventory only installed distribution metadata and run a finite CUDA diagnostic."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
IMAGE = "sha256:0bbae03be3d2c1340350d7eca6c9168b8735be57adfb61883fda8bad914d7bf4"


def main():
    code = """import importlib.metadata as m,json,sys
print(json.dumps({'python':sys.version,'prefix':sys.prefix,'distributions':[{'name':d.metadata['Name'],'version':d.version,'files':len(d.files or []),'wheel':d.read_text('WHEEL')} for d in m.distributions()]},sort_keys=True))
"""
    r = subprocess.run(
        ["docker", "run", "--rm", "--network=none", "--entrypoint", "python", IMAGE, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    (OUT / "gbar-distributions.json").write_text(r.stdout)
    d = json.loads(r.stdout)
    print(
        json.dumps(
            {"python": d["python"], "prefix": d["prefix"], "distributions": len(d["distributions"])}
        )
    )
    probe = """import json,torch,time
start=time.monotonic();assert torch.cuda.is_available()
x=torch.arange(1024,device='cuda',dtype=torch.float32);y=(x*x).sum();torch.cuda.synchronize()
assert y.item()==357389824
print(json.dumps({'cuda':True,'torch':torch.__version__,'device':torch.cuda.get_device_name(0),'checksum':y.item(),'seconds':time.monotonic()-start,'model_calls':0}))
"""
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            "scat-cdi-cuda-diagnostic-20261108",
            "--network=none",
            "--device",
            "nvidia.com/gpu=0",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            probe,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=40,
    )
    (OUT / "cdi-cuda-probe.log").write_text(r.stdout + r.stderr)
    assert r.returncode == 0, "cdi_cuda_diagnostic_failed"
    print(r.stdout.strip())


if __name__ == "__main__":
    main()
