"""A validation campaign cannot reset or bypass the persistent shared model cap."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_runtime.daily_budget import DailyBudget


def config(**overrides):
    return {"id": "closeout", "max_requests": 2, "max_units": 1000, "expires_at": 200, **overrides}


def test_campaign_is_shared_across_roles_restarts_and_failed_requests(tmp_path):
    path = tmp_path / "budget.sqlite"
    old = DailyBudget(path, clock=lambda: 100)
    old.finish(old.reserve("memory", b"old", 1), "unknown")
    budget = DailyBudget(path, campaign=config(), clock=lambda: 100)
    budget.finish(budget.reserve("memory", b"a", 1), "unknown")
    restarted = DailyBudget(path, campaign=config(), clock=lambda: 100)
    restarted.reserve("trial", b"b", 1)
    with pytest.raises(ValueError, match="campaign_budget_exhausted"):
        budget.reserve("author", b"c", 1)
    with pytest.raises(ValueError, match="immutable"):
        DailyBudget(path, campaign=config(max_requests=3), clock=lambda: 100)


def test_campaign_admission_is_atomic_and_global_limits_still_apply(tmp_path):
    budget = DailyBudget(
        tmp_path / "budget.sqlite", campaign=config(max_requests=1), clock=lambda: 100
    )

    def admit(_):
        try:
            budget.reserve("memory", b"a", 1)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(admit, range(8))) == 1
    global_cap = DailyBudget(
        tmp_path / "global.sqlite", lifetime_units=33, campaign=config(), clock=lambda: 100
    )
    with pytest.raises(ValueError, match="autonomy_model_budget_exhausted"):
        global_cap.reserve("memory", b"a", 1)


def test_campaign_units_expiry_and_invalid_policy(tmp_path):
    budget = DailyBudget(
        tmp_path / "units.sqlite", campaign=config(max_units=34), clock=lambda: 100
    )
    budget.reserve("memory", b"a", 1)
    with pytest.raises(ValueError, match="campaign_budget_exhausted"):
        budget.reserve("memory", b"a", 1)
    expired = DailyBudget(tmp_path / "expired.sqlite", campaign=config(), clock=lambda: 200)
    with pytest.raises(ValueError, match="campaign_budget_exhausted"):
        expired.reserve("memory", b"a", 1)
    with pytest.raises(ValueError, match="invalid_model_campaign"):
        DailyBudget(tmp_path / "invalid.sqlite", campaign=config(max_requests=True))
