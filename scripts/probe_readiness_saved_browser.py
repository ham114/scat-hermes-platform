"""Pass the existing website password in memory to a GET-only live browser check."""

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    temporary = ROOT / ".tmp/release-readiness/browser-real"
    temporary.mkdir(exist_ok=True)
    password = getpass.getpass("HYN website password: ")
    try:
        script = (
            "scripts/probe_readiness_readonly_browser.mjs"
            if sys.argv[1:] == ["--readonly"]
            else "scripts/probe_readiness_saved_browser.mjs"
        )
        result = subprocess.run(
            ["node", script],
            input=json.dumps({"username": "HYN", "password": password}),
            text=True,
            cwd=ROOT,
            env=os.environ | {"TEMP": str(temporary), "TMP": str(temporary)},
            check=False,
        )
    finally:
        password = None
    raise SystemExit(result.returncode)
