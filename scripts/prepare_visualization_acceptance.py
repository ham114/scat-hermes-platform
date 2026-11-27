"""Prepare/collect candidate release evidence offline. Never call a provider or deploy."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from visualization_runtime.release_acceptance import (
        assess,
        candidate_stamp,
        gate_evidence,
        load_session,
        prepare,
        read_json,
        round_evidence,
        save,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--name", required=True)
    for name in ("round", "gate", "assess"):
        command = commands.add_parser(name)
        command.add_argument("--session", type=Path, required=True)
        if name == "round":
            command.add_argument("--index", type=int, choices=(1, 2), required=True)
            command.add_argument("--report", type=Path, required=True)
        elif name == "gate":
            command.add_argument("--id", required=True)
            command.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            folder = prepare(ROOT, args.name, candidate_stamp(ROOT))
            result = {"session": str(folder), "paid_calls_started": 0, **assess(ROOT, folder)}
        else:
            folder, session = load_session(ROOT, args.session)
            if args.command == "round":
                plan = read_json(ROOT / "config/visualization-acceptance-v1.json")
                session["rounds"][str(args.index)] = round_evidence(
                    ROOT, session, plan, args.report
                )
                save(folder, session)
            elif args.command == "gate":
                gates = read_json(ROOT / "config/visualization-release-gates-v1.json")["gates"]
                gate = next((item for item in gates if item["id"] == args.id), None)
                if gate is None:
                    raise ValueError("unknown_acceptance_gate")
                session["gates"][args.id] = gate_evidence(folder, session, gate, args.evidence)
                save(folder, session)
            result = assess(ROOT, folder)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
        print(
            json.dumps(
                {"ready_for_release_review": False, "error": "acceptance_input_or_evidence_invalid"}
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command != "assess" or result["ready_for_release_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
