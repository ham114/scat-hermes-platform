"""Recover only the owned task using its lease; leave every service running."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
NAME = "scat-hermes-h7b-worker-1"


def main():
    before = json.loads(subprocess.check_output(["docker", "inspect", NAME]))[0]
    assert before["State"]["Health"]["Status"] == "healthy"
    if before["State"]["Health"]["Status"] == "healthy":
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "scat-hermes-h7b-actions-1",
                "/opt/conda/envs/scat-agent/bin/python",
                "-B",
                "-",
            ],
            input=(ROOT / "scripts/recover_gbar_forecast_result.py").read_text(),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        with (OUT / "null-truth-recovery.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr[-1800:]
        report = json.loads(result.stdout)
        (OUT / "null-truth-recovery.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    after = json.loads(subprocess.check_output(["docker", "inspect", NAME]))[0]
    assert after["Id"] == before["Id"] and after["State"]["Health"]["Status"] == "healthy"


if __name__ == "__main__":
    main()
