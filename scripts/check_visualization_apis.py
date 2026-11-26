"""Validate independent visualization API configuration; --live makes two paid probes."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from pydantic import ValidationError

    from visualization_runtime.preflight import run_preflight
    from visualization_runtime.settings import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="one small request per provider, no retries"
    )
    parser.add_argument("--role", choices=("code", "review", "both"), default="both")
    parser.add_argument(
        "--save-report", action="store_true", help="also save the short JSON report to disk"
    )
    args = parser.parse_args()
    try:
        settings = load_settings(ROOT)
    except ValidationError as error:
        fields = sorted({str(e["loc"][0]) for e in error.errors() if e["loc"]})
        print(
            json.dumps(
                {"passed": False, "error": "invalid_visualization_configuration", "fields": fields}
            )
        )
        return 2
    roles = ("code", "review") if args.role == "both" else (args.role,)
    report = asyncio.run(run_preflight(settings, live=args.live, roles=roles))
    report["checked_at"] = datetime.now(UTC).isoformat()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save_report:
        directory = ROOT / "output/visualization-api-preflight"
        directory.mkdir(parents=True, exist_ok=True)
        filename = directory / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        filename.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("Report: " + str(filename))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
