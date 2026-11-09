"""Explicit private author-service launch; never enabled on the business API."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda scat-agent")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18104)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged loopback port")
    import uvicorn

    uvicorn.run(
        "service.skill_authoring_api:configured_app",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )
