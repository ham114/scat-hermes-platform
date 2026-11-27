"""Conservative prepaid child allocation from the trusted native parent budget.

The frozen token ceiling is atomically reserved once, with the task insert.
Each provider request additionally shares the native request counter at admission.
Prepaid tokens remain charged after cancellation/unknown outcomes. Actual usage stays
in the visualization call ledger, never masquerading as native provider usage.
"""

import json
from pathlib import Path

from visualization_runtime.paths import confined_path
from visualization_runtime.persistence import require_atomic_journals
from visualization_runtime.store import JobConflict


def reserve(db, jobs, context, task_id, parent):
    if parent is None:
        return
    path, run_id = parent
    path = confined_path(jobs.workspace_root, Path(path))
    if not path.is_file():
        raise JobConflict("visualization_parent_budget_unavailable")
    db.execute("ATTACH DATABASE ? AS vis_parent", (str(path),))
    require_atomic_journals(db, "vis_parent")
    row = db.execute(
        "SELECT * FROM vis_parent.skill_run_budgets WHERE scope=? AND run_id=?",
        (context.scope_key, run_id),
    ).fetchone()
    if row is None:
        raise JobConflict("visualization_parent_budget_unavailable")
    limits = json.loads(row["limits_json"])
    tokens = jobs.limits.max_total_tokens
    # A completed conversation may authorize a later UI child using its remaining
    # allocation. The child has its own deadline, not a reset native run budget.
    if (
        row["exhausted"]
        or row["llm_calls"] >= limits["max_turns"]
        or row["charged_tokens"] + tokens > limits["max_llm_tokens"]
    ):
        raise JobConflict("visualization_parent_budget_exceeded")
    db.execute(
        "UPDATE vis_parent.skill_run_budgets SET charged_tokens=charged_tokens+? WHERE scope=? AND run_id=?",
        (tokens, context.scope_key, run_id),
    )
    db.execute(
        "INSERT INTO visualization_parent_allocations VALUES(?,?,?,?,?,?)",
        (task_id, context.scope_key, run_id, str(path), 0, tokens),
    )


def reserve_request(db, jobs, task_id):
    allocation = db.execute(
        "SELECT * FROM visualization_parent_allocations WHERE task_id=?", (task_id,)
    ).fetchone()
    if allocation is None:
        return  # P0-P3 maintenance jobs have no native parent.
    path = confined_path(jobs.workspace_root, Path(allocation["path"]))
    if not path.is_file():
        raise JobConflict("visualization_parent_budget_unavailable")
    db.execute("ATTACH DATABASE ? AS vis_parent", (str(path),))
    require_atomic_journals(db, "vis_parent")
    row = db.execute(
        "SELECT * FROM vis_parent.skill_run_budgets WHERE scope=? AND run_id=?",
        (allocation["scope"], allocation["run_id"]),
    ).fetchone()
    if (
        row is None
        or row["exhausted"]
        or row["llm_calls"] >= json.loads(row["limits_json"])["max_turns"]
    ):
        raise JobConflict("visualization_parent_budget_exceeded")
    db.execute(
        "UPDATE vis_parent.skill_run_budgets SET llm_calls=llm_calls+1 WHERE scope=? AND run_id=?",
        (allocation["scope"], allocation["run_id"]),
    )
    db.execute(
        "UPDATE visualization_parent_allocations SET reserved_requests=reserved_requests+1 WHERE task_id=?",
        (task_id,),
    )


def account_overrun(db, jobs, task_id):
    allocation = db.execute(
        "SELECT * FROM visualization_parent_allocations WHERE task_id=?", (task_id,)
    ).fetchone()
    if allocation is None:
        return
    charged = db.execute(
        "SELECT COALESCE(sum(charged_tokens),0) FROM visualization_provider_calls WHERE task_id=?",
        (task_id,),
    ).fetchone()[0]
    extra = max(0, charged - allocation["reserved_tokens"])
    if not extra:
        return
    path = confined_path(jobs.workspace_root, Path(allocation["path"]))
    if not path.is_file():
        raise JobConflict("visualization_parent_budget_unavailable")
    db.execute("ATTACH DATABASE ? AS vis_parent", (str(path),))
    require_atomic_journals(db, "vis_parent")
    db.execute(
        "UPDATE vis_parent.skill_run_budgets SET charged_tokens=charged_tokens+?,exhausted='visualization_provider_overrun' WHERE scope=? AND run_id=?",
        (extra, allocation["scope"], allocation["run_id"]),
    )
    db.execute(
        "UPDATE visualization_parent_allocations SET reserved_tokens=? WHERE task_id=?",
        (charged, task_id),
    )
