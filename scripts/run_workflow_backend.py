"""Run one previously admitted S5 numerical execution in the existing APP Conda."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda Python")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--generation", required=True, type=int)
    args = parser.parse_args()
    from workflow_runtime.backend import execute

    execute(ROOT, args.state, args.execution, args.generation)
