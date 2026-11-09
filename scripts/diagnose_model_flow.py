"""Inspect only this owned failed run's proposal validation; no database sampling or inference."""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-model-flow"
sys.path.insert(0, str(ROOT / "src"))


def main():
    from pydantic import ValidationError

    from workflow_runtime.catalog import Prepare

    run = "run_4c2acad33ea94451a13eaebf73e9ed1f"
    home = ROOT / "var/deploy/h7b/shared/homes/main"
    with sqlite3.connect("file:" + str(home / "skill-loads.sqlite") + "?mode=ro", uri=True) as db:
        sessions = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT session_id FROM skill_loads WHERE run_id=?", (run,)
            )
        }
    proposals = []
    with sqlite3.connect("file:" + str(home / "state.db") + "?mode=ro", uri=True) as db:
        for session in sessions:
            for (raw,) in db.execute(
                "SELECT tool_calls FROM messages WHERE session_id=? AND tool_calls IS NOT NULL",
                (session,),
            ):
                for call in json.loads(raw):
                    fn = call.get("function", call)
                    if fn.get("name") != "scat_workflow_prepare":
                        continue
                    args = fn["arguments"]
                    args = json.loads(args) if isinstance(args, str) else args
                    try:
                        Prepare.model_validate(args)
                        errors = []
                    except ValidationError as exc:
                        errors = exc.errors(
                            include_input=False, include_url=False, include_context=False
                        )
                    proposals.append(
                        {
                            "argument_keys": sorted(args),
                            "proposal": args.get("proposal"),
                            "errors": errors,
                        }
                    )
    report = {
        "status": "diagnosed",
        "planning_run_id": run,
        "proposals": proposals,
        "new_llm_calls": 0,
        "new_gpu_posts": 0,
    }
    (OUT / "failed-proposal-diagnosis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
