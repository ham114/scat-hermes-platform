"""Read-only DB evidence for the live browser run; no user text or credentials exported."""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-writable-live"


def database(path):
    db = sqlite3.connect("file:" + str(ROOT / path) + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def main():
    live = json.loads((OUT / "browser-live-receipt.json").read_text())
    controls = json.loads((OUT / "browser-controls-receipt.json").read_text())
    assert live["status"] == controls["status"] == "passed"
    report = {
        "status": "running",
        "tasks": {},
        "budget_accounting": "Shared job budgets already import parent use; do not add parent diagnostic again.",
    }
    with database("var/deploy/h7b/shared/state/tasks.sqlite") as db:
        for label, identity in [
            ("completed", live["task_id"]),
            ("revoked", controls["revoked_task_id"]),
            ("cancelled", controls["cancel_task_id"]),
        ]:
            row = db.execute(
                "SELECT task_id,state,run_id,snapshot FROM model_tasks WHERE task_id=?", (identity,)
            ).fetchone()
            assert row
            snapshot = json.loads(row["snapshot"])
            job = db.execute(
                "SELECT state,generation,parent_imported FROM skill_jobs WHERE task_id=?",
                (identity,),
            ).fetchone()
            sessions = [
                dict(r)
                for r in db.execute(
                    "SELECT native_run,native_session,generation,revision FROM skill_job_sessions WHERE task_id=?",
                    (identity,),
                )
            ]
            calls = [
                dict(r)
                for r in db.execute(
                    "SELECT id,state,calls,output_sha FROM skill_backend_executions WHERE task_id=?",
                    (identity,),
                )
            ]
            budget = db.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,provider_cached_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (row["run_id"],),
            ).fetchone()
            entry = {
                "task_state": row["state"],
                "job": dict(job) if job else None,
                "independent_native_sessions": sessions,
                "backend_executions": calls,
                "family_budget": dict(budget) if budget else None,
            }
            if label == "completed":
                assert job["state"] == "succeeded" and sessions
                assert all(s["native_run"] != live["parent_run_id"] for s in sessions)
                assert len(calls) == 3 and all(
                    c["calls"] == 1 and c["state"] == "succeeded" for c in calls
                )
                entry["parent_budget_bound"] = (
                    snapshot["skills"].get("parent_budget", {}).get("run_id")
                    == live["parent_run_id"]
                )
                assert entry["parent_budget_bound"] and job["parent_imported"] == 1
            elif label == "revoked":
                assert row["state"] == "revoked" and not calls
            else:
                assert job["state"] == "cancelled" and not calls
            report["tasks"][label] = entry
    with database("var/deploy/h7b/shared/homes/main/skill-loads.sqlite") as db:
        parent = db.execute(
            "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens FROM skill_run_budgets WHERE run_id=?",
            (live["parent_run_id"],),
        ).fetchone()
        report["parent_diagnostic_not_extra_charge"] = dict(parent) if parent else None
    budgets = [v["family_budget"] for v in report["tasks"].values() if v["family_budget"]]
    report["family_totals"] = {
        k: sum(b[k] for b in budgets)
        for k in [
            "llm_calls",
            "tool_calls",
            "charged_tokens",
            "provider_input_tokens",
            "provider_output_tokens",
        ]
    }
    report["status"] = "passed"
    (OUT / "backend-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "audit": "passed",
                "independent_worker": True,
                "success_backend_calls": 3,
                "revocation_backend_calls": 0,
                "cancelled_backend_calls": 0,
                "family_totals": report["family_totals"],
            }
        )
    )


if __name__ == "__main__":
    main()
