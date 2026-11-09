"""Real private RPC catalog: admitted ZYJ scope works, mismatched HYN run is rejected."""
import json
import os
import sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from deployment_runtime.client import credential

if __name__ == "__main__":
    os.umask(0o077)
    assert json.loads((OUT / "scope-fix/deployment-result.json").read_text())["status"] == "applied_behind_gate"
    case = json.loads((OUT / "natural-materialization-report.json").read_text())["cases"][0]
    rows = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    scope = next(r["scope"] for r in rows if r["thread_id"] == case["thread_id"])
    mismatched = json.loads((OUT / "natural-experiment.json").read_text())["run_id"]
    with httpx.Client(base_url="http://127.0.0.1:18123", trust_env=False, timeout=45, headers={"Authorization": "Bearer " + credential(ROOT, "var/deploy/h7b/secrets/rpc-main.key")}) as client:
        results = []
        for source, expected in [(case["run_id"], "ok"), (mismatched, "rejected")]:
            response = client.post("/v1/actions/workflows", json={"operation": "scat_workflow_catalog", "arguments": {}, "scope": scope, "source_run_id": source, "proposal_key": "readiness-owner-probe"})
            response.raise_for_status()
            value = response.json()
            assert value["status"] == expected, value.get("status")
            results.append({"matching_run": source == case["run_id"], "status": value["status"]})
    report = {"status": "passed", "kind": "live_role_authenticated_private_rpc_not_model_call", "cases": results, "new_model_calls": 0, "model_supplied_owner_accepted": False}
    (OUT / "scope-fix/live-control-scope.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
