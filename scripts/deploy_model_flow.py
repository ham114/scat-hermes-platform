"""Apply the scoped prediction planning fix using the existing backed-up release flow."""

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import deploy_gbar_production as gpu

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-model-flow"


def main():
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    gpu.OUT = OUT
    gpu.deploy.OUT = OUT
    gpu.FILES = [*gpu.FILES, "src/workflow_runtime/contracts.py", "scripts/run_workflow_tool.py"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "apply"])
    args = parser.parse_args()
    if args.action == "build":
        gpu.stage()
        plan = json.loads((OUT / "deployment-plan.json").read_text())
        plan["gpu_configuration_changed"] = False
        plan["purpose"] = "complete_model_flow_without_manual_recovery"
        (OUT / "deployment-plan.json").write_text(json.dumps(plan, indent=2))
        return
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    assert (
        hashlib.sha256((ROOT / "var/gpu/production.json").read_bytes()).hexdigest()
        == plan["gpu_registration_file_sha256"]
    )
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as source:
        assert not source.execute(
            "SELECT 1 FROM gpu_requests WHERE state IN ('running','unknown')"
        ).fetchone()
        backup = OUT / ("private-gpu-receipts-before-switch-" + plan["release"] + ".sqlite")
        assert not backup.exists()
        with sqlite3.connect(backup) as target:
            source.backup(target)
        backup.chmod(0o600)
    gpu.deploy.apply()


if __name__ == "__main__":
    main()
