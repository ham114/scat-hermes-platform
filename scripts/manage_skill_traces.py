"""Trusted local maintenance CLI. No HTTP/LLM authorization or arbitrary SQL."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from hermes_runtime.skill_policy import SkillPolicy, load_policy
    from hermes_runtime.skills import checked_path
    from hermes_runtime.trace_lifecycle import TraceLifecycle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument(
        "--action",
        choices=["maintain", "read", "hold", "release", "preview", "apply"],
        required=True,
    )
    parser.add_argument("--scope")
    parser.add_argument("--run")
    parser.add_argument("--candidate-policy")
    parser.add_argument("--preview-sha256")
    args = parser.parse_args()
    path = checked_path(ROOT, args.state)
    if not path.is_file():
        parser.error("Existing trace database required")
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT policy FROM skill_trace_policy WHERE id=1").fetchone()
    policy = SkillPolicy.model_validate(json.loads(row[0])) if row else load_policy(ROOT)
    lifecycle = TraceLifecycle(path, policy, workspace_root=ROOT)
    if args.action in {"hold", "release", "read"}:
        if not args.scope or not args.run:
            parser.error("Trusted scope/run binding required")
        if args.action == "read":
            result = lifecycle.read(args.scope, args.run)
        else:
            lifecycle.hold(args.scope, args.run, args.action == "hold")
            result = {"status": "updated", "held": args.action == "hold"}
    elif args.action in {"preview", "apply"}:
        if not args.candidate_policy:
            parser.error("Candidate policy required")
        candidate = load_policy(ROOT, Path(args.candidate_policy))
        result = lifecycle.preview_policy(candidate)
        if args.action == "apply":
            lifecycle.apply_policy(candidate, args.preview_sha256)
            result["applied"] = True
    else:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            result = lifecycle.maintain(db)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda Python required")
    main()
