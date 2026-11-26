"""Additive schema migration and shared recovery of provider admissions."""


def migrate_foundation(db):
    if "resume_phase" not in {
        row[1] for row in db.execute("PRAGMA table_info(visualization_jobs)")
    }:
        db.execute("ALTER TABLE visualization_jobs ADD COLUMN resume_phase TEXT")
    schema = """
        CREATE TABLE IF NOT EXISTS visualization_job_budgets(
            task_id TEXT PRIMARY KEY, limits_json TEXT NOT NULL, deadline REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS visualization_snapshots(
            snapshot_id TEXT PRIMARY KEY, owner TEXT NOT NULL, thread TEXT NOT NULL,
            request_id TEXT NOT NULL, references_sha TEXT NOT NULL, payload_sha TEXT NOT NULL,
            bytes INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0, UNIQUE(owner,thread,request_id));
        CREATE TABLE IF NOT EXISTS visualization_snapshot_sources(
            snapshot_id TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,kind,source_id));
        CREATE TABLE IF NOT EXISTS visualization_revoked_sources(
            owner TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
            PRIMARY KEY(owner,kind,source_id));
        CREATE TABLE IF NOT EXISTS visualization_provider_calls(
            call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, call_key TEXT NOT NULL,
            generation INTEGER NOT NULL, role TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, reserved_tokens INTEGER NOT NULL, charged_tokens INTEGER NOT NULL,
            day TEXT NOT NULL, created REAL NOT NULL, finished REAL,
            result TEXT, result_sha TEXT, error_code TEXT, UNIQUE(task_id,call_key));
        CREATE INDEX IF NOT EXISTS ix_visualization_provider_task ON visualization_provider_calls(task_id);
        CREATE INDEX IF NOT EXISTS ix_visualization_provider_day ON visualization_provider_calls(day);
        CREATE TABLE IF NOT EXISTS visualization_attempts(
            attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL, generation INTEGER NOT NULL, code_call_id TEXT NOT NULL,
            source TEXT NOT NULL, source_sha TEXT NOT NULL, created REAL NOT NULL,
            UNIQUE(task_id,step_key), UNIQUE(task_id,ordinal));
        CREATE TABLE IF NOT EXISTS visualization_resources(
            resource_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
            generation INTEGER NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL)
    """
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)


def fence_pending_calls(db, task_id, now):
    # An admission that was never marked sending has no external effect. A
    # sending request may have been billed and must never be silently replayed.
    db.execute(
        "UPDATE visualization_provider_calls SET state='aborted',charged_tokens=0,"
        "finished=?,error_code='not_sent' WHERE task_id=? AND state='reserved'",
        (now, task_id),
    )
    db.execute(
        "UPDATE visualization_provider_calls SET state='unknown',finished=?,"
        "error_code='execution_interrupted' WHERE task_id=? AND state='sending'",
        (now, task_id),
    )
