"""Trusted receipt ingestion and finite automatic author scheduling, never publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from hermes_runtime.autonomy_config import private_text
from hermes_runtime.registry import RunRegistry, ThreadNotFoundError
from hermes_runtime.results import ToolResultStore
from hermes_runtime.skills import checked_path


def sha(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_automatic_content(candidate, content):
    """Automatic drafts may only document operations actually observed in their sources."""
    operations = set()
    for item in candidate["evidence"]:
        operations.update(json.loads(item["summary"])["operations"])
    changes = content.get("changes") if isinstance(content, dict) else None
    if not isinstance(changes, list) or len(changes) != 1:
        raise ValueError("automatic_author_requires_one_skill")
    change = changes[0]
    if isinstance(change, dict) and any(
        path.startswith("scripts/") for path in change.get("files", {})
    ):
        raise ValueError("automatic_scripts_require_separate_opt_in")
    if (
        not isinstance(change, dict)
        or not isinstance(change.get("name"), str)
        or not change["name"].startswith("auto-")
        or not change.get("allowed_operations")
        or set(change["allowed_operations"]) - operations
    ):
        raise ValueError("automatic_author_verified_operations_only")


class LearningLedger:
    def __init__(self, root, policy, *, clock=time.time, read_only=False):
        self.root, self.policy, self.clock = root.resolve(), policy, clock
        self.path = checked_path(self.root, policy.state)
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise ValueError("learning_state_not_initialized")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS verified_experiences(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, pattern TEXT NOT NULL,
                    input_sha TEXT NOT NULL, summary TEXT NOT NULL, evidence_sha TEXT NOT NULL,
                    created REAL NOT NULL, job TEXT);
                CREATE TABLE IF NOT EXISTS automatic_skill_jobs(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, pattern TEXT NOT NULL,
                    day TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL,
                    evidence TEXT NOT NULL, candidate TEXT, draft TEXT, trial TEXT,
                    error TEXT, native_run TEXT);
                CREATE TABLE IF NOT EXISTS automatic_skill_retries(
                    parent TEXT PRIMARY KEY, child TEXT NOT NULL UNIQUE);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = (
            sqlite3.connect("file:" + self.path.as_posix() + "?mode=ro", uri=True, timeout=10)
            if self.read_only
            else sqlite3.connect(self.path, timeout=10)
        )
        db.row_factory = sqlite3.Row
        return db

    def record_verified(self, *, identity, owner, operations, input_sha, evidence_sha):
        # This method has no public HTTP route. Only the collectors below call it.
        if (
            not operations
            or len(operations) > 32
            or any(
                not isinstance(op, str) or not op.startswith(("mcp__scat__", "scat_"))
                for op in operations
            )
        ):
            raise ValueError("invalid_verified_operation_pattern")
        for digest in (input_sha, evidence_sha):
            if len(digest) != 64 or set(digest) - set("0123456789abcdef"):
                raise ValueError("invalid_verified_source_digest")
        pattern = sha(operations)
        summary = json.dumps(
            {
                "operations": operations,
                "success": "server_verified_receipts",
                "privacy": "no_arguments_outputs_or_user_messages",
                "requirements": [
                    "validate required inputs",
                    "use actual tool references",
                    "stop on empty/error/unknown",
                    "report source and limits",
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with closing(self.connect()) as db, db:
            old = db.execute(
                "SELECT * FROM verified_experiences WHERE id=?", (identity,)
            ).fetchone()
            if old and (
                old["evidence_sha"] != evidence_sha
                or old["owner"] != owner
                or old["input_sha"] != input_sha
                or old["pattern"] != pattern
            ):
                raise ValueError("verified_source_changed")
            db.execute(
                "INSERT OR IGNORE INTO verified_experiences VALUES(?,?,?,?,?,?,?,NULL)",
                (identity, owner, pattern, input_sha, summary, evidence_sha, self.clock()),
            )

    def reserve(self):
        if not self.policy.enabled:
            return None
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT count(*) FROM automatic_skill_jobs WHERE day=?", (day,)
                ).fetchone()[0]
                >= self.policy.max_drafts_per_day
            ):
                return None
            # A prior uncertain dispatch blocks a second dispatch until explicit reconciliation.
            if db.execute(
                "SELECT 1 FROM automatic_skill_jobs WHERE state IN ('reserved','authoring','unknown','trial_running')"
            ).fetchone():
                return None
            groups = db.execute(
                "SELECT owner,pattern FROM verified_experiences WHERE job IS NULL "
                "GROUP BY owner,pattern HAVING count(*)>=? AND count(DISTINCT input_sha)>=? "
                "ORDER BY min(created)",
                (self.policy.min_successes, self.policy.min_distinct_inputs),
            ).fetchall()
            for group in groups:
                recent = db.execute(
                    "SELECT max(created) FROM automatic_skill_jobs WHERE owner=? AND pattern=?",
                    (group["owner"], group["pattern"]),
                ).fetchone()[0]
                if recent is not None and now - recent < self.policy.cooldown_hours * 3600:
                    continue
                rows = db.execute(
                    "SELECT * FROM verified_experiences WHERE owner=? AND pattern=? AND job IS NULL ORDER BY created,id LIMIT 8",
                    (group["owner"], group["pattern"]),
                ).fetchall()
                identity = uuid4().hex
                evidence = [
                    {
                        "kind": "task_review",
                        "reference": row["id"],
                        "revision": row["evidence_sha"],
                        "summary": row["summary"],
                    }
                    for row in rows
                ]
                db.execute(
                    "INSERT INTO automatic_skill_jobs VALUES(?,?,?,?,?,'reserved',?,NULL,NULL,NULL,NULL,NULL)",
                    (identity, group["owner"], group["pattern"], day, now, json.dumps(evidence)),
                )
                db.executemany(
                    "UPDATE verified_experiences SET job=? WHERE id=?",
                    [(identity, row["id"]) for row in rows],
                )
                return self.get(identity, db=db)
        return None

    def get(self, identity, *, db=None):
        if db is None:
            with closing(self.connect()) as connection:
                return self.get(identity, db=connection)
        row = dict(
            db.execute("SELECT * FROM automatic_skill_jobs WHERE id=?", (identity,)).fetchone()
        )
        row["evidence"] = json.loads(row["evidence"])
        return row

    def retry_failed(self, identity):
        """Trusted operator only, after independent process and candidate checks."""
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(identity, db=db)
            if row["state"] != "failed" or row["draft"] or row["trial"]:
                raise ValueError("retry_requires_known_failure_without_draft_or_trial")
            if db.execute(
                "SELECT 1 FROM automatic_skill_retries WHERE parent=?", (identity,)
            ).fetchone():
                raise ValueError("manual_retry_already_created")
            if db.execute(
                "SELECT 1 FROM automatic_skill_jobs WHERE state IN ('reserved','authoring','unknown','trial_running')"
            ).fetchone():
                raise ValueError("another_learning_job_unresolved")
            if (
                db.execute(
                    "SELECT count(*) FROM automatic_skill_jobs WHERE day=?", (day,)
                ).fetchone()[0]
                >= self.policy.max_drafts_per_day
            ):
                raise ValueError("daily_learning_budget_exhausted")
            child = uuid4().hex
            db.execute(
                "INSERT INTO automatic_skill_jobs VALUES(?,?,?,?,?,'reserved',?,NULL,NULL,NULL,NULL,NULL)",
                (child, row["owner"], row["pattern"], day, now, json.dumps(row["evidence"])),
            )
            db.execute("INSERT INTO automatic_skill_retries VALUES(?,?)", (identity, child))
            return self.get(child, db=db)

    def update(self, identity, state, **values):
        if set(values) - {"candidate", "draft", "trial", "error", "native_run"}:
            raise ValueError("invalid_learning_update")
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE automatic_skill_jobs SET state=?"
                + "".join("," + key + "=?" for key in values)
                + " WHERE id=?",
                (state, *values.values(), identity),
            )

    def status(self, owner):
        with closing(self.connect()) as db:
            jobs = [
                dict(row)
                for row in db.execute(
                    "SELECT id,state,created,candidate,draft,trial,error FROM automatic_skill_jobs WHERE owner=? ORDER BY created DESC LIMIT 100",
                    (owner,),
                )
            ]
            count = db.execute(
                "SELECT count(*) FROM verified_experiences WHERE owner=?", (owner,)
            ).fetchone()[0]
            patterns = [
                dict(row)
                for row in db.execute(
                    "SELECT pattern,count(*) AS successes,count(DISTINCT input_sha) AS distinct_inputs FROM verified_experiences WHERE owner=? GROUP BY pattern ORDER BY pattern LIMIT 100",
                    (owner,),
                )
            ]
        return {
            "enabled": self.policy.enabled,
            "auto_trials": self.policy.auto_trials,
            "patterns": patterns,
            "scheduler_observed": False,
            "verified_experiences": count,
            "jobs": jobs,
            "automatic_publication": False,
            "thresholds": {
                k: getattr(self.policy, k)
                for k in (
                    "min_successes",
                    "min_distinct_inputs",
                    "cooldown_hours",
                    "max_drafts_per_day",
                )
            },
        }


async def collect_readonly(root, policy, ledger, *, transport=None):
    """Validate native completion, owner scope, trace arguments and receipt SHA together."""
    paths = [
        checked_path(root, getattr(policy, name)) for name in ("registry", "results", "traces")
    ]
    if not all(path.is_file() for path in paths):
        return 0
    registry = RunRegistry(paths[0], workspace_root=root)
    results = ToolResultStore(paths[1], workspace_root=root)
    with closing(sqlite3.connect(paths[1])) as db:
        runs = db.execute(
            "SELECT DISTINCT scope_key,run_id FROM tool_results WHERE expires_at>? ORDER BY created_at DESC LIMIT 1000",
            (time.time(),),
        ).fetchall()
    count = 0
    async with httpx.AsyncClient(
        base_url=policy.native_url,
        trust_env=False,
        transport=transport,
        headers={"Authorization": "Bearer " + private_text(root, policy.native_key)},
        timeout=5,
    ) as http:
        for scope, run in runs:
            try:
                context = registry.require_scope(policy.owner, scope)
                registry.require(context, run)
                identity = "readonly:" + sha([scope, run])
                with closing(ledger.connect()) as db:
                    if db.execute(
                        "SELECT 1 FROM verified_experiences WHERE id=?", (identity,)
                    ).fetchone():
                        continue
                response = await http.get(
                    "/v1/runs/" + run, headers={"X-Hermes-Session-Key": scope}
                )
                response.raise_for_status()
                terminal = response.json()
                if (
                    terminal.get("run_id") != run
                    or terminal.get("status") != "completed"
                    or terminal.get("partial")
                    or terminal.get("interrupted")
                    or terminal.get("completed") is False
                ):
                    continue
                receipts = [
                    results.get(scope, run, ref["result_id"]) for ref in results.list(scope, run)
                ]
                with closing(sqlite3.connect(paths[2])) as db:
                    db.row_factory = sqlite3.Row
                    traces = [
                        dict(row)
                        for row in db.execute(
                            "SELECT operation,arguments_sha256,outcome,result_sha256,bundle_sha256 FROM skill_operations WHERE scope=? AND run_id=? ORDER BY id",
                            (scope, run),
                        )
                    ]
                # Only learn wholly successful read-only traces; proposal/control calls are excluded.
                operations = ["mcp__scat__" + ref["operation"] for ref in receipts]
                if (
                    not receipts
                    or len(traces) != len(receipts)
                    or [r["operation"] for r in traces] != operations
                    or any(r["outcome"] != "returned" or not r["result_sha256"] for r in traces)
                ):
                    continue
                ledger.record_verified(
                    identity=identity,
                    owner=policy.owner,
                    operations=operations,
                    input_sha=sha([r["arguments_sha256"] for r in traces]),
                    evidence_sha=sha(
                        {
                            "receipts": [
                                {k: r[k] for k in ("result_id", "payload_sha256")} for r in receipts
                            ],
                            "traces": traces,
                        }
                    ),
                )
                count += 1
            except (ThreadNotFoundError, LookupError, ValueError, httpx.HTTPError, sqlite3.Error):
                continue
    return count


def collect_workflows(root, policy, ledger):
    """Recheck persisted backend hashes and saved/read result references, never model prose."""
    path = checked_path(root, policy.tasks)
    if not path.is_file():
        return 0
    from application.skill_job_factory import create_skill_tasks
    from application.skill_worker import completion_reference

    app = create_skill_tasks(root, path, workflows_enabled=True)
    registry = RunRegistry(checked_path(root, policy.registry), workspace_root=root)
    with closing(app.store.connect()) as db:
        identities = [
            row[0]
            for row in db.execute(
                "SELECT t.task_id FROM model_tasks t JOIN skill_jobs j USING(task_id) "
                "WHERE t.state='finished' AND j.state='succeeded' ORDER BY t.updated DESC LIMIT 100"
            )
        ]
    count = 0
    for identity in identities:
        try:
            with closing(app.store.connect()) as db:
                raw = db.execute(
                    "SELECT * FROM model_tasks WHERE task_id=?", (identity,)
                ).fetchone()
                row = app.store._decode(raw)
            registry.require_scope(policy.owner, row["scope"])
            reference = completion_reference(app, row)
            if reference is None:
                continue
            with closing(app.store.connect()) as db:
                receipts = [
                    dict(r)
                    for r in db.execute(
                        "SELECT operation,state,reference FROM model_step_receipts WHERE scope=? AND run=? ORDER BY rowid",
                        (row["scope"], row["run_id"]),
                    )
                ]
                events = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT data FROM skill_job_events WHERE task_id=? AND kind='step_returned' ORDER BY id",
                        (identity,),
                    )
                ]
            # Include verified read-back steps (which intentionally have no effect
            # receipt). Repeated trials share an operation family, not raw values.
            operations = list(
                dict.fromkeys(
                    event["operation"]
                    for event in events
                    if isinstance(event.get("operation"), str)
                )
            )
            ledger.record_verified(
                identity="task:" + identity,
                owner=policy.owner,
                operations=operations,
                input_sha=sha(row["snapshot"]["proposal"]),
                evidence_sha=sha(
                    {
                        "snapshot": row["sha256"],
                        "receipts": receipts,
                        "events": events,
                        "completion_reference": reference,
                    }
                ),
            )
            count += 1
        except (LookupError, ValueError, KeyError, OSError, sqlite3.Error):
            continue
    return count
