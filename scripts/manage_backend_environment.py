"""Export/verify existing service environments; never install or create an environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
INTERPRETERS = {
    "scat-agent": "/home/h3c/anaconda3/envs/scat-agent/bin/python",
    "scat-hermes": "/home/h3c/anaconda3/envs/scat-hermes/bin/python",
}


def main():
    from backend_ops.environment import dump, inventory, verify_inputs, write_inputs

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["snapshot", "verify", "inspect"])
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(inventory(ROOT)))
        return 0
    if sys.executable != INTERPRETERS["scat-agent"]:
        raise ValueError("use_existing_scat_agent")
    rows = {}
    work = ROOT / ".tmp/h7a/environment"
    work.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(work),
        "TMP": str(work),
        "TMPDIR": str(work),
    }
    for name, python in INTERPRETERS.items():
        output = subprocess.check_output(
            [python, "-B", str(Path(__file__).resolve()), "inspect"],
            cwd=ROOT,
            env=env,
            timeout=60,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        actual = json.loads(output)
        folder = ROOT / "environments/backend-h7a" / name
        if args.action == "snapshot":
            rows[name] = write_inputs(folder, actual)
        else:
            rows[name] = {"matches": verify_inputs(folder, actual)}
    report = {"action": args.action, "environments": rows, "installs": 0, "new_environments": 0}
    if args.action == "snapshot":
        dump(ROOT / "environments/backend-h7a/manifest.json", json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if all(r.get("matches", True) for r in rows.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({"status": "failed", "code": "environment_export_or_verification_failed"}))
        raise SystemExit(1) from None
