"""Run S2 deterministic synthetic scenarios inside an owned temporary workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from hermes_runtime.skill_evaluation import evaluate_cases
    from hermes_runtime.skills import checked_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report_path = checked_path(ROOT, args.report)
    if report_path.exists():
        raise ValueError("report_already_exists")
    work = ROOT / ".tmp/s2c" / ("evaluation-" + uuid4().hex)
    work.mkdir(parents=True)
    try:
        report = evaluate_cases(ROOT, work)
    finally:
        assert work.resolve().is_relative_to(ROOT / ".tmp/s2c") and not work.is_symlink()
        shutil.rmtree(work)
    report["temporary_workspace_removed"] = not work.exists()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "domains": report["domains"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
