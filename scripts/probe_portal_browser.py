"""Local existing-Conda/Node read-only browser verification; all credentials stay in memory."""

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from preview_remote_frontend import SSHConnection

ROOT = Path(__file__).resolve().parents[1]


def main():
    connection = SSHConnection(getpass.getpass("SSH password: "))
    web_password = None
    try:
        connection.get()
        web_password = getpass.getpass("HYN website password: ")
        _, out, _ = connection.client.exec_command(
            "cat /home/h3c/project/python/scat-hermes-platform/var/deploy/h7b/secrets/author.key",
            timeout=15,
        )
        key = out.read().decode().strip()
        assert out.channel.recv_exit_status() == 0 and len(key) >= 32
        result = subprocess.run(
            ["node", str(ROOT / "scripts/probe_portal_browser.mjs")],
            input=json.dumps({"username": "HYN", "password": web_password, "key": key}),
            text=True,
            cwd=ROOT,
            env=os.environ
            | {
                "SCAT_PORTAL_READBACK": "1",
                "TEMP": str(ROOT / ".tmp/platform-closeout"),
                "TMP": str(ROOT / ".tmp/platform-closeout"),
            },
            check=False,
        )
        key = None
        return result.returncode
    finally:
        web_password = None
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
