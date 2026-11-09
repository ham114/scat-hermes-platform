"""Owned durable CPU backend process; no network and no legacy model graph."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    from application.skill_cpu_backend import execute_backend

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--generation", required=True, type=int)
    args = parser.parse_args()
    execute_backend(ROOT, args.state, args.execution, args.generation)
