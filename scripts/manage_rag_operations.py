"""Trusted operator CLI for RAG status/reconciliation/cancel; not a model tool."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise ValueError("use_existing_anaconda")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--action", choices=["status", "reconcile", "cancel"], default="status")
    args = parser.parse_args()
    path = (ROOT / args.state).resolve()
    if not path.is_relative_to((ROOT / "var").resolve()) or not path.is_file():
        raise ValueError("untrusted_rag_state_path")
    sys.path.insert(0, str(ROOT / "src"))
    from hermes_runtime.rag_operation import RagOperation

    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM rag_invocations WHERE id=?", (args.id,)).fetchone()
        if not row:
            print(json.dumps({"status": "not_found"}))
            return
        row = dict(row)
        checker = object.__new__(RagOperation)
        checker.root = ROOT
        alive = checker.owned_process(row)
        if args.action == "cancel" and row["status"] in {"starting", "running"}:
            if alive:
                os.killpg(row["pid"], signal.SIGTERM)
            db.execute(
                "UPDATE rag_invocations SET status='unknown',error='operator_cancel_verify_final_state' WHERE id=?",
                (args.id,),
            )
            row["status"] = "unknown"
        elif args.action == "reconcile" and row["status"] in {"starting", "running"} and not alive:
            db.execute(
                "UPDATE rag_invocations SET status='unknown',error='interrupted_do_not_replay' WHERE id=?",
                (args.id,),
            )
            row["status"] = "unknown"
        print(
            json.dumps(
                {
                    "request_id": args.id,
                    "status": row["status"],
                    "owned_process_alive": alive,
                    "automatic_replay": False,
                }
            )
        )


if __name__ == "__main__":
    main()
