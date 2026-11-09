"""Read only this round's explicitly created QA conversation after a planning gap."""
import json
import os
import sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.portal_identity import private_key

if __name__ == "__main__":
    os.umask(0o077)
    report = json.loads((OUT / "natural-materialization-report.json").read_text())
    case = report["cases"][0]
    with httpx.Client(base_url="http://127.0.0.1:18159/api/v1/hermes", timeout=30, trust_env=False, headers={"Origin": "https://172.26.67.148:8444", "X-Portal-Authenticated-User": case["user"], "X-Portal-Gateway-Token": private_key(ROOT, "var/deploy/portal/keys/front.key")}) as client:
        path = "/threads/" + case["thread_id"]
        response = client.get(path + "/runs/" + case["run_id"])
        response.raise_for_status()
        state = response.json()
        tasks = client.get(path + "/model-tasks").json()["items"]
    (OUT / "private/materialization-planning-gap.json").write_text(json.dumps({"state": state, "tasks": tasks}, ensure_ascii=False, indent=2))
    report["status"] = "gaps_found"
    case.update(status="no_prepared_task", prepared_count=len(tasks))
    (OUT / "natural-materialization-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"status": state["status"], "output": state.get("output"), "prepared_count": len(tasks)}, ensure_ascii=False))
