"""Attach the memory/author relay to the same finite QA cap; retain all existing limits."""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30)


def counts():
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        return db.execute(
            "SELECT count(*),coalesce(sum(reserved),0),coalesce(sum(state='reserved'),0) FROM model_calls"
        ).fetchone()


def main():
    os.umask(0o077)
    assert not (OUT / "proxy-campaign-install.json").exists()
    before = counts()
    assert before == (129, 6940664, 0), "proxy_changed_review_budget_before_install"
    assert "27 passed" in (OUT / "proxy-campaign-tests.log").read_text()
    rows = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    selected = next(r for r in rows if r["user"] == "admin" and r["index"] == 3)
    descriptor = ROOT / "var/deploy/validation-campaign/proxy-scope.json"
    descriptor.write_text(
        json.dumps({"scope": selected["scope"], "campaign_id": "release-readiness-20261109-A"})
    )
    descriptor.chmod(0o600)
    backup = OUT / "private/proxy-before-campaign"
    backup.mkdir(exist_ok=False)
    source = ROOT / "scripts/run_autonomy_proxy.py"
    old = source.read_text().replace(
        "    from hermes_runtime.validation_proxy import validation_reserver\n", ""
    )
    old = old.replace("        validation_reserve=validation_reserver(ROOT),\n", "")
    (backup / "run_autonomy_proxy.py").write_text(old)
    source_budget = ROOT / "src/hermes_runtime/daily_budget.py"
    old_budget = (
        source_budget.read_text()
        .replace("        validation_reserve=None,\n", "")
        .replace("        self.validation_reserve = validation_reserve\n", "")
        .replace(
            "            if self.validation_reserve is not None:\n                self.validation_reserve(units, role)\n",
            "",
        )
    )
    (backup / "daily_budget.py").write_text(old_budget)
    dropin = Path(
        "/home/h3c/.config/systemd/user/scat-memory-model-proxy.service.d/10-release-readiness-budget.conf"
    )
    assert not dropin.exists()
    dropin.parent.mkdir(exist_ok=True)
    dropin.write_text(
        "[Service]\nEnvironment=SCAT_VALIDATION_CAMPAIGN_FILE=var/deploy/validation-campaign/active.json\nEnvironment=SCAT_VALIDATION_PROXY_SCOPE_FILE=var/deploy/validation-campaign/proxy-scope.json\n"
    )
    try:
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "restart", "scat-memory-model-proxy.service"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(30):
            try:
                with opener.open("http://127.0.0.1:18142/health", timeout=1) as response:
                    assert json.load(response)["status"] == "ready"
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(0.25)
    except Exception:
        source.write_text(old)
        source_budget.write_text(old_budget)
        dropin.unlink()
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "restart", "scat-memory-model-proxy.service"])
        raise
    report = {
        "status": "enabled",
        "old_caps_unchanged": True,
        "before_counts": before,
        "after_counts": counts(),
        "scope": selected["scope"],
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [source, source_budget, ROOT / "src/hermes_runtime/validation_proxy.py"]
        },
        "new_model_calls_during_install": 0,
    }
    (OUT / "proxy-campaign-install.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
