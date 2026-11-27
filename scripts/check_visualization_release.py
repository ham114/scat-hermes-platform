"""Offline P6 preflight/backup. No provider calls, service changes or credentials in output."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from visualization_runtime.release import audit, backup, verify_backup
    from visualization_runtime.settings import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=os.environ.get("HERMES_TASK_STATE_FILE", "var/platform/model-tasks.sqlite"),
    )
    parser.add_argument(
        "--parent",
        default=os.environ.get("SCAT_PARENT_BUDGET_PATH")
        or os.environ.get("SCAT_VIS_PARENT_BUDGET_PATH"),
    )
    parser.add_argument("--memory")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backup-to")
    parser.add_argument("--verify-backup")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    if args.verify_backup:
        print(json.dumps({"backup_valid": verify_backup(ROOT, Path(args.verify_backup))}))
        return 0
    settings = load_settings(ROOT)
    report = audit(
        ROOT,
        settings,
        Path(args.state),
        Path(args.parent) if args.parent else None,
        memory_path=Path(args.memory) if args.memory else None,
        worker=args.worker,
    )
    if args.backup_to:
        if not args.writers_stopped:
            parser.error("backup requires --writers-stopped after stopping all state writers")
        databases = {"tasks": Path(args.state)}
        if args.parent:
            databases["parent"] = Path(args.parent)
        if args.memory:
            databases["memory"] = Path(args.memory)
        backup(ROOT, Path(args.backup_to), databases, ROOT / settings.artifact_root)
        report["backup_created"] = True
    print(json.dumps(report, indent=2))
    return 0 if report.get("backup_created") or report["local_prerequisites_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
