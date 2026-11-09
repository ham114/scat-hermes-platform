"""Read-only failure audit; no model calls, credentials or business payloads in output."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-receipt-diagnosis"
CASES = [
    ("343b8430-55d0-40bf-b6e1-cb0b4ca65f3c", "run_ff948b7e76124769a2e91470ce2a5857"),
    ("343b8430-55d0-40bf-b6e1-cb0b4ca65f3c", "run_21f3a9703abb4cc29accceaabba3f4f6"),
    ("343b8430-55d0-40bf-b6e1-cb0b4ca65f3c", "run_023fd1196bed452d8d86bfea68c6c341"),
    ("343b8430-55d0-40bf-b6e1-cb0b4ca65f3c", "run_fc98a67fd339498b85c0e0bf40c379fd"),
    ("d450ce8d-b3b2-4cd7-aad6-f787f26954c4", "run_06bd7285abad4e36a3c2e6647fc92c52"),
]


def main():
    def inspect(role):
        return json.loads(
            subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-" + role + "-1"])
        )[0]

    platform = inspect("platform")
    token = dict(x.split("=", 1) for x in platform["Config"]["Env"] if "=" in x)["AUTH_SECRET"]
    native_key = (ROOT / "var/deploy/h7b/secrets/hermes.key").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(url, headers):
        with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
            return json.load(response)

    records = []
    with (
        sqlite3.connect(
            "file:" + str(ROOT / "var/deploy/h7b/shared/state/bindings.sqlite") + "?mode=ro",
            uri=True,
        ) as bindings,
        sqlite3.connect(
            "file:" + str(ROOT / "var/deploy/h7b/shared/homes/main/skill-loads.sqlite") + "?mode=ro",
            uri=True,
        ) as budget,
    ):
        budget.row_factory = sqlite3.Row
        for tid, rid in CASES:
            base = f"http://127.0.0.1:18120/api/v1/hermes/threads/{tid}/runs/{rid}"
            headers = {"Authorization": "Bearer " + token}
            state, refs = get(base, headers), get(base + "/results", headers)["items"]
            scope = bindings.execute("SELECT scope_key FROM runtime_runs WHERE run_id=?", (rid,)).fetchone()[0]
            record = {
                "run_id": rid,
                "status": state["status"],
                "receipt_count": len(refs),
                "receipt_operations": [r["operation"] for r in refs],
            }
            try:
                native = get(
                    "http://127.0.0.1:18121/v1/runs/" + rid,
                    {"Authorization": "Bearer " + native_key, "X-Hermes-Session-Key": scope},
                )
                error = native.get("error") or ""
                match = re.search(r"HTTP (\d{3})", error)
                record["provider_http_status"] = int(match[1]) if match else None
                request = re.search(r"request_id: ([a-f0-9-]{36})", error)
                record["provider_request_id"] = request[1] if request else None
            except urllib.error.HTTPError as exc:
                record["native_status_http"] = exc.code
            row = budget.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (rid,),
            ).fetchone()
            record["budget"] = dict(row) if row else None
            records.append(record)
    native = inspect("hermes-main")
    logs = subprocess.run(
        ["docker", "logs", "--tail", "300", native["Name"].lstrip("/")],
        capture_output=True,
        text=True,
        check=True,
    )
    logs = logs.stdout + logs.stderr
    report = {
        "date": "2026-11-07",
        "cases": records,
        "native_restart_count": native["RestartCount"],
        "native_started_at": native["State"]["StartedAt"],
        "native_health": native["State"].get("Health", {}).get("Status"),
        "receipt_publish_warnings_in_last_300_log_lines": logs.count("readonly_receipt_unavailable"),
        "provider_500_log_occurrences": logs.count("HTTP 500"),
        "boot_sha256": hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest(),
        "new_prediction_jobs": 0,
        "raw_trajectory_or_reply_saved": False,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "backend-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
