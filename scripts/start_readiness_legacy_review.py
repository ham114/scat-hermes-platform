"""Start the read-only renderer harness on loopback only; never alter the legacy site."""
import json
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
if __name__ == "__main__":
    os.umask(0o077)
    assert not (OUT / "legacy-review-process.json").exists()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", 18136)) != 0
    log = (OUT / "legacy-review-service.log").open("x")
    process = subprocess.Popen(["/home/h3c/anaconda3/envs/scat-agent/bin/python", "-B", "-m", "streamlit", "run", "scripts/serve_readiness_legacy_review.py", "--server.address", "127.0.0.1", "--server.port", "18136", "--server.headless", "true", "--browser.gatherUsageStats", "false"], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    result = {"pid": process.pid, "port": 18136, "loopback_only": True, "new_model_calls": 0, "legacy_site_changed": False}
    (OUT / "legacy-review-process.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
