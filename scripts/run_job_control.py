"""Start the private background Hermes controller, not a business task."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Conda required")
    import uvicorn

    from deployment_runtime.client import credential
    from deployment_runtime.jobs import create_app

    uvicorn.run(
        create_app(credential(ROOT, "var/deploy/h7b/secrets/job-control.key")),
        host="127.0.0.1",
        port=18122,
        access_log=False,
    )
