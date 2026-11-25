"""Systemd ExecCondition: check the RAG switch without loading models or dependencies."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def enabled(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("rag_configuration_outside_project")
    value = json.loads(path.read_text(encoding="utf-8")).get("enabled", True)
    if type(value) is not bool:
        raise ValueError("invalid_rag_enabled_flag")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="services/rag/qwen3-production-v1.json")
    args = parser.parse_args()
    raise SystemExit(0 if enabled(ROOT, args.config) else 1)
