"""Read existing real results through HTTPS with an in-memory website credential."""

import getpass
import json
import os
import subprocess
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    temporary = root / ".tmp/business-parity/node-temp"
    temporary.mkdir(parents=True, exist_ok=True)
    password = getpass.getpass("HYN website password: ")
    try:
        result = subprocess.run(
            ["node", str(root / "scripts/probe_business_parity_browser.mjs")],
            input=json.dumps({"username": "HYN", "password": password}),
            text=True,
            cwd=root,
            check=False,
            env=os.environ | {"TEMP": str(temporary), "TMP": str(temporary)},
        )
    finally:
        password = None
    raise SystemExit(result.returncode)
