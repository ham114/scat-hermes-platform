"""Recover interrupted synthetic profiles while holding the exclusive author lock."""

from __future__ import annotations

import importlib.util
import json
import re
import time
from contextlib import closing


async def recover_interrupted(store):
    with closing(store.releases.connect()) as db:
        pending = {row[0] for row in db.execute("SELECT id FROM trials WHERE state='running'")}
    if not pending:
        return 0
    root = store.root
    spec = importlib.util.spec_from_file_location(
        "scat_author_recovery_evaluator", root / "scripts/evaluate_skills_s6.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from dotenv import dotenv_values

    for path in (root / "docs/validation").glob("20261024-S7A-trial-*.json"):
        report = json.loads(path.read_text())
        if report.get("trial_id") not in pending:
            continue
        identity = report.get("identity", "")
        if not isinstance(identity, str) or not re.fullmatch("[a-f0-9]{10}", identity):
            raise ValueError("invalid_interrupted_trial_identity")
        home = root / ".tmp/s7a" / ("native-" + identity)
        if not home.exists():
            continue
        if home.is_symlink() or home.resolve().parent != (root / ".tmp/s7a").resolve():
            raise ValueError("invalid_interrupted_trial_home")
        evaluation = object.__new__(module.Evaluation)
        evaluation.identity = identity
        evaluation.home, evaluation.temp_root = home, root / ".tmp/s7a"
        evaluation.children = []
        evaluation.values = dotenv_values(root / ".env")
        evaluation.collections = [
            "analysis_runs_s6_" + identity,
            "analysis_artifacts_s6_" + identity,
        ]
        evaluation.report_path, evaluation.report = path, report
        evaluation.attempts = report.get("attempts", [])
        evaluation.expected_domains = report.get("required_domains")
        evaluation.started = time.monotonic() - float(report.get("elapsed_seconds", 0))
        evaluation.report.update(
            status="failed", error_type="InterruptedTrial", recovery_cleanup=True
        )
        # Cancellation and process ownership checks are the same finite cleanup used
        # by normal trials. A failure stops startup; it never permits a second trial.
        await evaluation.cleanup()
    return store.recover_trials()
