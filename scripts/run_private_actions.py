"""Serve loopback-only, role-authenticated MCP and backend actions in APP Conda."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Conda required")
    import uvicorn

    from application.readonly_extensions import build_extended_business_application
    from deployment_runtime.actions import create_app
    from deployment_runtime.client import credential

    keys = {
        role: credential(ROOT, "var/deploy/h7b/secrets/rpc-" + role + ".key")
        for role in ["main", "worker", "platform"]
    }
    uvicorn.run(
        create_app(keys, build_extended_business_application()),
        host="127.0.0.1",
        port=int(os.environ.get("SCAT_ACTION_PORT", "18123")),
        access_log=False,
    )
