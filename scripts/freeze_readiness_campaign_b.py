"""Freeze the explicitly approved additional 48/4M batch, preserving batch A unchanged."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.validation_campaign import ValidationCampaign


def main():
    os.umask(0o077)
    target = OUT / "native-campaign-b.json"
    assert not target.exists(), "additional_campaign_already_frozen"
    old = json.loads((OUT / "native-campaign.json").read_text())
    with sqlite3.connect("file:" + str(ROOT / "var/autonomy/validation/state.sqlite") + "?mode=ro", uri=True) as db:
        before = db.execute("SELECT count(*),sum(units) FROM validation_calls WHERE campaign=?", (old["id"],)).fetchone()
    assert before == (96, 6079891)
    active = ROOT / "var/deploy/validation-campaign/active.json"
    assert json.loads(active.read_text()) == old
    policy = {**old, "id": "release-readiness-20261109-B", "max_calls": 48, "max_units": 4000000, "expires_at": time.time() + 86400}
    ValidationCampaign(ROOT, policy)
    target.write_text(json.dumps(policy, indent=2) + "\n")
    descriptor = ROOT / "var/deploy/validation-campaign/proxy-scope.json"
    proxy = json.loads(descriptor.read_text())
    proxy["campaign_id"] = policy["id"]
    pending = descriptor.with_suffix(".pending")
    pending.write_text(json.dumps(proxy))
    pending.chmod(0o600)
    pending.replace(descriptor)
    pending = active.with_suffix(".pending")
    pending.write_text(json.dumps(policy))
    pending.chmod(0o600)
    pending.replace(active)
    # Relay captures its scope descriptor at startup; restart only the idle relay.
    import subprocess
    with sqlite3.connect("file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM model_calls WHERE state='reserved'").fetchone()[0] == 0
    subprocess.run(["systemctl", "--user", "restart", "scat-memory-model-proxy.service"], check=True, timeout=45)
    result = {"status": "additional_authorized_campaign_frozen", "batch_a_calls": before[0], "batch_a_units": before[1], "batch_b_max_calls": 48, "batch_b_max_units": 4000000, "authorized_destination": "https://api.deepseek.com", "authorization": "user explicitly approved additional finite validation", "prior_ledger_reset": False, "new_model_calls_during_setup": 0}
    (OUT / "campaign-b-authorization.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
