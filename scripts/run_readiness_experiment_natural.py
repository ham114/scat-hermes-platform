"""One off-grid real CPU experiment via ordinary Main language and confirmed native job."""
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.portal_identity import private_key
from run_readiness_natural import budget


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    os.umask(0o077)
    target = OUT / "natural-experiment.json"
    assert not target.exists(), "already_admitted_no_replay"
    assert budget()["reserved_calls"] <= 86
    plan = json.loads((OUT / "asof-plan.json").read_text())
    cutoff = datetime.fromisoformat(plan["as_of"]).replace(tzinfo=UTC).isoformat()
    rows = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    thread = next(t["thread_id"] for t in rows if t["user"] == "HYN" and t["index"] == 5)
    message = f"请为批次scat20161015_20161021中100002准备一个有限CPU实验：任意截止时间为{cutoff}，严格使用截止时刻及之前最后200个真实观测点，从截止时刻向后10、30、60秒做历史回测；比较constant-velocity-heading版本1与kalman-cv版本1，每个配置仅1次，仅1航班和1起点。实验中只补建这一航班的phase_segments派生结果，并由真实制品生成评估和报告。不要使用GPU，不处理整批，先准备方案等待确认。"
    report = {"status": "admission_pending", "thread_id": thread, "request_id": str(uuid4()), "question": message, "as_of_utc": cutoff, "source_time_convention": "SCAT naive source timestamps represent UTC", "budget_before": budget(), "confirmations": 0, "new_gpu_calls": 0}
    save(target, report)
    with httpx.Client(base_url="http://127.0.0.1:18159/api/v1/hermes", trust_env=False, timeout=45, headers={"Origin": "https://172.26.67.148:8444", "X-Portal-Authenticated-User": "HYN", "X-Portal-Gateway-Token": private_key(ROOT, "var/deploy/portal/keys/front.key")}) as http:
        before = {t["task_id"] for t in http.get(f"/threads/{thread}/model-tasks").json()["items"]}
        response = http.post(f"/threads/{thread}/runs", json={"request_id": report["request_id"], "message": message})
        response.raise_for_status()
        report.update(status="planning", run_id=response.json()["run_id"])
        save(target, report)
        endpoint = f"/threads/{thread}/runs/{report['run_id']}"
        for _ in range(150):
            time.sleep(2)
            state = http.get(endpoint).json()
            if state["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
        else:
            http.post(endpoint + "/stop", json={})
            raise RuntimeError("planning_timeout_stop_requested")
        save(OUT / "private/natural-experiment-planning.json", state)
        assert state["status"] == "completed"
        tasks = [t for t in http.get(f"/threads/{thread}/model-tasks").json()["items"] if t["task_id"] not in before]
        report["prepared_task_count"] = len(tasks)
        save(target, report)
        assert len(tasks) == 1, "unexpected_prepared_tasks_not_confirmed"
        task = tasks[0]
        proposal = task["workflow"]["proposal"]
        save(OUT / "private/natural-experiment-proposal.json", task)
        assert proposal["kind"] == "experiment" and proposal["external_ids"] == ["100002"] and proposal["max_flights"] == 1
        assert proposal["forecast_origin"] == "as_of" and datetime.fromisoformat(proposal["as_of"].replace("Z", "+00:00")) == datetime.fromisoformat(cutoff)
        assert proposal["observation_last_n"] == 200 and proposal["horizons_s"] == [10, 30, 60] and proposal["repeats"] == 1
        assert {m["model"] for m in proposal["models"]} == {"constant-velocity-heading", "kalman-cv"}
        assert proposal["materializations"] == ["phase_segments"] and proposal["max_cutoffs_per_flight"] == 1
        report.update(status="confirmation_intent", task_id=task["task_id"], approved_snapshot=task["snapshot_sha256"], confirmations=1)
        save(target, report)
        task_endpoint = f"/threads/{thread}/model-tasks/{task['task_id']}"
        response = http.post(task_endpoint + "/confirm", json={"confirmed": True, "snapshot_sha256": task["snapshot_sha256"]})
        response.raise_for_status()
        report["status"] = "executing"
        save(target, report)
        for _ in range(300):
            time.sleep(2)
            response = http.get(task_endpoint)
            response.raise_for_status()
            task = response.json()
            job = task["skill_job"]
            if job["state"] in {"succeeded", "failed", "cancelled", "unknown", "interrupted"}:
                break
        save(OUT / "private/natural-experiment-result.json", task)
        report.update(status="passed" if job["state"] == "succeeded" else "gaps_found", job_state=job["state"], budget=budget())
        report["artifacts"] = []
        for identity in task.get("run", {}).get("result_artifact_ids", []):
            first = http.get(task_endpoint + "/artifacts/" + identity)
            first.raise_for_status()
            artifact = first.json()
            save(OUT / "private" / ("experiment-artifact-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + ".json"), artifact)
            again = http.get(task_endpoint + "/artifacts/" + identity).json()
            assert again["payload_sha256"] == artifact["payload_sha256"]
            report["artifacts"].append({"artifact_id": identity, "sha256": artifact["payload_sha256"], "reopen_identical": True, "blocks": [b["type"] for b in artifact["analysis_result"]["blocks"]]})
        save(target, report)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
