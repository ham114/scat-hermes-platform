import json
import sqlite3

import pytest

from hermes_runtime import validation_campaign
from hermes_runtime.daily_budget import DailyBudget
from hermes_runtime.validation_campaign import CampaignExhausted, ValidationCampaign
from hermes_runtime.validation_proxy import validation_reserver


def test_native_and_proxy_share_the_same_cap_and_failed_calls_stay_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(validation_campaign, "ROOT", tmp_path)
    scope = "scat:" + "a" * 64
    policy = {
        "schema_version": 1,
        "id": "qa-proxy",
        "scopes": [scope],
        "max_calls": 2,
        "max_units": 1000,
        "expires_at": 9999999999,
    }
    (tmp_path / "active.json").write_text(json.dumps(policy))
    (tmp_path / "proxy.json").write_text(json.dumps({"scope": scope, "campaign_id": "qa-proxy"}))
    monkeypatch.setenv("SCAT_VALIDATION_CAMPAIGN_FILE", "active.json")
    monkeypatch.setenv("SCAT_VALIDATION_PROXY_SCOPE_FILE", "proxy.json")
    native = ValidationCampaign(tmp_path, policy)
    native.reserve(scope, "native", 100)
    daily = DailyBudget(tmp_path / "daily.sqlite", validation_reserve=validation_reserver(tmp_path))
    identity = daily.reserve("memory", b"controlled synthetic preference", 20)
    daily.finish(identity, "unknown")
    with pytest.raises(ValueError, match="validation_campaign_exhausted"):
        daily.reserve("author", b"next", 20)
    with pytest.raises(CampaignExhausted):
        native.reserve(scope, "another_native", 1)
    with sqlite3.connect(tmp_path / "daily.sqlite") as db:
        assert db.execute("SELECT count(*),state FROM model_calls").fetchone() == (1, "unknown")
    (tmp_path / "active.json").write_text(json.dumps({"schema_version": 1, "status": "retired"}))
    daily.reserve("memory", b"ordinary_after_qa_retired", 20)


def test_proxy_scope_mismatch_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(validation_campaign, "ROOT", tmp_path)
    (tmp_path / "proxy.json").write_text(
        json.dumps({"scope": "scat:" + "a" * 64, "campaign_id": "one"})
    )
    (tmp_path / "active.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "two",
                "scopes": ["scat:" + "b" * 64],
                "max_calls": 1,
                "max_units": 100,
                "expires_at": 9999999999,
            }
        )
    )
    monkeypatch.setenv("SCAT_VALIDATION_CAMPAIGN_FILE", "active.json")
    monkeypatch.setenv("SCAT_VALIDATION_PROXY_SCOPE_FILE", "proxy.json")
    with pytest.raises(ValueError, match="not_selected"):
        validation_reserver(tmp_path)(10, "memory")
