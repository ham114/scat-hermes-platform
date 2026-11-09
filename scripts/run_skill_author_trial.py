"""Trusted finite synthetic candidate evaluation, launched only by the author service."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def run(actor, identity):
    from evaluate_skills_s6 import Evaluation

    from hermes_runtime.authoring_store import AuthoringStore

    store = AuthoringStore(ROOT)
    trial = store.trial(actor, identity)
    if trial["state"] != "running":
        raise ValueError("trial_not_running")
    revision = store.get(actor, trial["draft"], trial["revision"])
    evaluation = None
    script_report = None
    try:
        from hermes_runtime.script_trials import run_script_cases

        script_report = run_script_cases(ROOT, store.releases.load(trial["candidate"]), identity)
        if script_report["status"] != "passed":
            raise ValueError("isolated_script_cases_failed")
        evaluation = Evaluation(trial | {"changes": revision["content"]["changes"]})
        await evaluation.run()
        report = evaluation.report
    except BaseException as exc:  # noqa: BLE001 -- retain cancellation/failure after evaluator cleanup
        report = (evaluation.report if evaluation else {}) | {
            "status": "failed",
            "error_type": type(exc).__name__,
        }
    report["trial_id"] = identity
    report["script_cases"] = script_report
    store.finish_trial(identity, report)
    print(
        json.dumps({"trial": identity, "state": store.trial(actor, identity)["state"]}), flush=True
    )


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing Anaconda scat-agent")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--trial", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.actor, args.trial))
