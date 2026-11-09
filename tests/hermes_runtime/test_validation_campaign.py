import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_runtime import validation_campaign
from hermes_runtime.skill_budget import BudgetExhausted, RunBudget
from hermes_runtime.skill_policy import Budget
from hermes_runtime.validation_campaign import CampaignExhausted, ValidationCampaign


def policy(**changes):
    return {
        "schema_version": 1,
        "id": "acceptance-a",
        "scopes": ["scat:" + "a" * 64, "scat:" + "b" * 64],
        "max_calls": 3,
        "max_units": 500,
        "expires_at": 200,
        **changes,
    }


def test_shared_atomic_cap_is_persistent_and_cannot_be_raised_or_extended(tmp_path):
    phase = ValidationCampaign(tmp_path, policy(), clock=lambda: 100)

    def reserve(i):
        try:
            phase.reserve(policy()["scopes"][i % 2], f"run_{i}", 150)
            return True
        except CampaignExhausted:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(8))) == 3
    reopened = ValidationCampaign(tmp_path, policy(), clock=lambda: 101)
    with pytest.raises(CampaignExhausted):
        reopened.reserve(policy()["scopes"][0], "run_restart", 1)
    for change in [{"max_units": 600}, {"max_calls": 4}, {"expires_at": 300}]:
        with pytest.raises(ValueError, match="immutable"):
            ValidationCampaign(tmp_path, policy(**change), clock=lambda: 100)


def test_native_requests_stop_before_upstream_and_other_user_scopes_are_unaffected(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(validation_campaign, "ROOT", tmp_path)
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(policy(max_calls=1, max_units=10000, expires_at=9999999999)))
    monkeypatch.setenv("SCAT_VALIDATION_CAMPAIGN_FILE", "campaign.json")
    main = RunBudget(tmp_path / "runs.sqlite", policy()["scopes"][0], "run_one", Budget(), "policy")
    main.reserve_llm({"messages": [], "max_tokens": 10})
    other = RunBudget(
        tmp_path / "runs.sqlite", policy()["scopes"][1], "run_two", Budget(), "policy"
    )
    with pytest.raises(BudgetExhausted, match="validation_campaign"):
        other.reserve_llm({"messages": [], "max_tokens": 10})
    unrelated = RunBudget(
        tmp_path / "runs.sqlite", "scat:" + "c" * 64, "run_real_user", Budget(), "policy"
    )
    unrelated.reserve_llm({"messages": [], "max_tokens": 10})
    with sqlite3.connect(tmp_path / "var/autonomy/validation/state.sqlite") as db:
        assert db.execute("SELECT count(*) FROM validation_calls").fetchone()[0] == 1
