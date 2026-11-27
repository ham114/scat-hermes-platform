"""Acceptance restarts must retain every earlier attempt's paid-call accounting."""

import json
import sqlite3

import pytest

from visualization_runtime.acceptance import prior_campaign_usage


def attempt(root, name, *, parent=None, calls=1, unfinished=None):
    folder = root / ".tmp/visualization-p6" / name
    folder.mkdir(parents=True)
    (folder / "frozen-plan.json").write_text('{"cases":[]}', encoding="utf-8")
    (folder / "report.json").write_text(json.dumps({"prior_run": parent}), encoding="utf-8")
    with sqlite3.connect(folder / "tasks.sqlite") as db:
        db.execute("CREATE TABLE visualization_jobs(state TEXT)")
        db.execute(
            "INSERT INTO visualization_jobs VALUES(?)",
            ("running" if unfinished == "job" else "failed",),
        )
        db.execute(
            "CREATE TABLE visualization_provider_calls(call_key TEXT,state TEXT,charged_tokens INTEGER)"
        )
        db.executemany(
            "INSERT INTO visualization_provider_calls VALUES('paid',?,10)",
            [("unknown" if unfinished == "call" else "succeeded",)] * calls,
        )
        db.execute("INSERT INTO visualization_provider_calls VALUES('fixture-seed','succeeded',0)")
        db.execute("INSERT INTO visualization_provider_calls VALUES('never-sent','aborted',0)")
    return folder


def test_full_ancestry_counts_all_calls_and_tokens_once(tmp_path):
    attempt(tmp_path, "first", calls=2)
    attempt(tmp_path, "second", parent="first", calls=3)
    assert prior_campaign_usage(tmp_path, "second", {"cases": []}, current_run="third") == (5, 50)


def test_parent_cycle_cannot_hide_or_reset_spending(tmp_path):
    attempt(tmp_path, "first", parent="second")
    attempt(tmp_path, "second", parent="first")
    with pytest.raises(ValueError, match="chain"):
        prior_campaign_usage(tmp_path, "second", {"cases": []}, current_run="third")


@pytest.mark.parametrize("unfinished", ["job", "call"])
def test_unfinished_or_unknown_parent_cannot_be_replayed(tmp_path, unfinished):
    attempt(tmp_path, "first", unfinished=unfinished)
    with pytest.raises(ValueError, match="not_finished"):
        prior_campaign_usage(tmp_path, "first", {"cases": []}, current_run="second")
