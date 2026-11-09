"""Trusted local GPU receipt inspection; no model tool or replay operation."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from workflow_runtime.gpu import GPURequests, registration

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "resolve-unknown"))
    parser.add_argument("--request-id")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--remote-termination-confirmed", action="store_true")
    args = parser.parse_args()
    config = registration(ROOT, required=True)
    ledger = GPURequests(ROOT, config)
    if args.action == "resolve-unknown":
        ledger.resolve(
            args.request_id,
            args.expected_sha256,
            remote_termination_confirmed=args.remote_termination_confirmed,
        )
    print(
        json.dumps(
            {"gpu": config.pin(), "requests": ledger.records()}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
