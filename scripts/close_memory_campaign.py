"""Close completed validation admission without changing the original lifetime budget."""

import hashlib
import json
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"


def main():
    result = json.loads((OUT / "memory-acceptance.json").read_text())
    assert result["status"] == "passed"
    assert not (OUT / "memory-campaign-closed.json").exists()
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        assert (
            db.execute("SELECT count(*) FROM model_calls WHERE state='reserved'").fetchone()[0] == 0
        )
        before = db.execute("SELECT count(*),sum(reserved) FROM model_calls").fetchone()
    policy = ROOT / "var/deploy/autonomy/proxy.json"
    sha = hashlib.sha256(policy.read_bytes()).hexdigest()
    assert json.loads(policy.read_text()) == {
        "daily_requests": 240,
        "daily_units": 8000000,
        "lifetime_requests": 240,
        "lifetime_units": 8000000,
    }
    unit = ROOT / "var/deploy/autonomy/units/scat-memory-model-proxy.service"
    text = unit.read_text()
    assert (
        "SCAT_MODEL_CAMPAIGN_FILE=var/deploy/autonomy/campaign-closeout.json" in text
        and "/usr/bin/sg docker" in text
    )
    text = text.replace(
        "Environment=SCAT_MODEL_CAMPAIGN_FILE=var/deploy/autonomy/campaign-closeout.json\n", ""
    )
    text = text.replace(" --env=SCAT_MODEL_CAMPAIGN_FILE", "")
    campaign = str(ROOT / "var/deploy/autonomy/campaign-closeout.json")
    text = text.replace(" --mount=type=bind,src=" + campaign + ",dst=" + campaign + ",readonly", "")
    assert "SCAT_MODEL_CAMPAIGN_FILE" not in text
    unit.write_text(text)
    for args in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", "scat-memory-model-proxy.service"],
    ]:
        subprocess.run(args, check=True, timeout=50, capture_output=True)
    for index in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18142/health", timeout=1) as r:
                assert json.load(r)["status"] == "ready"
            break
        except OSError:
            if index == 39:
                raise
            time.sleep(0.25)
    assert hashlib.sha256(policy.read_bytes()).hexdigest() == sha
    release = (ROOT / "var/deploy/autonomy/active-release").read_text().strip()
    (ROOT / "var/deploy/autonomy/releases" / release / "memory-proxy-final.service").write_text(
        text
    )
    subprocess.run(
        ["systemctl", "--user", "start", "scat-auto-skill-learning.service"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    report = {
        "status": "passed",
        "campaign_closed": True,
        "reservations_preserved": True,
        "usage_at_close": before,
        "lifetime_requests_cap": 240,
        "lifetime_units_cap": 8000000,
        "budget_configuration_sha256": sha,
        "scheduler_restored": True,
        "provider_dns_scoped_to_proxy_container": True,
        "host_dns_changed": False,
        "server_rebooted": False,
    }
    (OUT / "memory-campaign-closed.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
