"""Resume the existing paid job after explicit additional aggregate authorization; retain steps."""
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.portal_identity import private_key


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    os.umask(0o077)
    assert (OUT / "campaign-b-authorization.json").exists()
    record = OUT / "natural-experiment.json"
    report = json.loads(record.read_text())
    assert report["status"] == "paused_aggregate_call_limit"
    client = httpx.Client(base_url="http://127.0.0.1:18159/api/v1/hermes", trust_env=False, timeout=45, headers={"Origin": "https://172.26.67.148:8444", "X-Portal-Authenticated-User": "HYN", "X-Portal-Gateway-Token": private_key(ROOT, "var/deploy/portal/keys/front.key")})
    endpoint = f"/threads/{report['thread_id']}/model-tasks/{report['task_id']}"
    with client as http:
        response = http.get(endpoint)
        response.raise_for_status()
        task = response.json()
        assert task["skill_job"]["state"] == "paused"
        report["resume_intent"] = {"snapshot_sha256": task["snapshot_sha256"], "limits": {"max_turns": 24, "max_llm_tokens": 1000000, "max_tool_calls": 80, "wall_seconds": 1800}, "uses_existing_task_and_backend_receipts": True}
        save(OUT / "natural-experiment-before-resume.json", report)
        report["status"] = "resume_send_intent"
        save(record, report)
        response = http.post(endpoint + "/budget", json={"snapshot_sha256": task["snapshot_sha256"], "limits": report["resume_intent"]["limits"], "confirmed": True})
        if response.status_code == 422:
            raise RuntimeError("resume_schema_rejected_no_replay")
        response.raise_for_status()
        report["status"] = "resumed"
        save(record, report)
        for _ in range(240):
            time.sleep(2)
            response = http.get(endpoint)
            response.raise_for_status()
            task = response.json()
            if task["skill_job"]["state"] in {"succeeded", "partial", "failed", "cancelled", "unknown", "paused"}:
                break
        save(OUT / "private/natural-experiment-result.json", task)
        report.update(status="passed" if task["skill_job"]["state"] == "succeeded" else "gaps_found", job_state=task["skill_job"]["state"], artifacts=[])
        for identity in task.get("run", {}).get("result_artifact_ids", []):
            response = http.get(endpoint + "/artifacts/" + identity)
            response.raise_for_status()
            artifact = response.json()
            repeated = http.get(endpoint + "/artifacts/" + identity).json()
            assert artifact["payload_sha256"] == repeated["payload_sha256"]
            save(OUT / "private" / ("experiment-artifact-" + __import__('hashlib').sha256(identity.encode()).hexdigest()[:16] + ".json"), artifact)
            report["artifacts"].append({"artifact_id": identity, "sha256": artifact["payload_sha256"], "reopen_identical": True, "blocks": [b["type"] for b in artifact["analysis_result"]["blocks"]]})
        save(record, report)
        print(json.dumps({k: v for k, v in report.items() if k != "question"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
