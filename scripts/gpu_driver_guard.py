"""Fail closed on driver/CDI drift before starting the pinned Conda GPU service."""
import hashlib
import os
from pathlib import Path

if "580.173.02" not in Path("/proc/driver/nvidia/version").read_text():
    raise SystemExit("GPU kernel version changed; refresh the reviewed CDI configuration")
if hashlib.sha256(Path("/etc/cdi/scat-nvidia.json").read_bytes()).hexdigest() != "2a8752f7140605aac289d71bca0375df8072f18d3b16af05d94866eeed1294b1":
    raise SystemExit("GPU CDI specification changed; review before service startup")
python = "/opt/conda/envs/scat-gbar/bin/python"
os.execv(python, [python, "-B", "-m", "bentoml", "serve", "gbar_service.service:GBARV23Service", "--host", "0.0.0.0", "--port", "3000"])
