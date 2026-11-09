"""Freeze one aggregate native validation cap and empty owner-scoped acceptance threads."""

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from application.context import ExecutionContext
from hermes_runtime.portal_identity import private_key
from hermes_runtime.validation_campaign import ValidationCampaign


def main():
    os.umask(0o077)
    out = ROOT / "var/validation/20261108-release-readiness"
    private = out / "private"
    private.mkdir(exist_ok=True)
    target = out / "native-campaign.json"
    assert not target.exists(), "campaign_already_frozen_no_reset"
    key = private_key(ROOT, "var/deploy/portal/keys/front.key")
    users = json.loads((ROOT / "var/deploy/portal/identities.json").read_text())["principals"]
    threads = []
    with httpx.Client(base_url="http://127.0.0.1:18159", trust_env=False, timeout=15) as client:
        for user, principal in users.items():
            for index in range(6):
                response = client.post(
                    "/api/v1/hermes/threads",
                    json={},
                    headers={
                        "X-Portal-Authenticated-User": user,
                        "X-Portal-Gateway-Token": key,
                        "Origin": "https://172.26.67.148:8444",
                    },
                )
                response.raise_for_status()
                thread = response.json()["thread_id"]
                threads.append(
                    {
                        "user": user,
                        "owner": principal["owner_id"],
                        "index": index,
                        "thread_id": thread,
                        "scope": ExecutionContext(principal["owner_id"], thread).scope_key,
                    }
                )
                (private / "native-campaign-threads.json").write_text(json.dumps(threads, indent=2))
    policy = {
        "schema_version": 1,
        "id": "release-readiness-20261109-A",
        "scopes": [r["scope"] for r in threads],
        "max_calls": 96,
        "max_units": 8000000,
        "expires_at": time.time() + 86400,
    }
    ValidationCampaign(ROOT, policy)
    target.write_text(json.dumps(policy, indent=2) + "\n")
    active = ROOT / "var/deploy/validation-campaign/active.json"
    assert json.loads(active.read_text()) == {"schema_version": 1, "status": "retired"}
    staging = active.with_suffix(".pending")
    staging.write_text(json.dumps(policy))
    staging.replace(active)
    print(
        json.dumps(
            {
                "status": "campaign_frozen",
                "threads": len(threads),
                "max_calls": 96,
                "max_units": 8000000,
                "new_model_calls": 0,
                "failed_unknown_reservations_refunded": False,
            }
        )
    )


if __name__ == "__main__":
    main()
