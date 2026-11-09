"""Ordinary materialization planning and scoped report reuse; no extra GPU calls."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.portal_identity import private_key


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    os.umask(0o077)
    retest = sys.argv[1:] == ["--retest-after-scope-fix"]
    target = OUT / ("natural-materialization-report-r2.json" if retest else "natural-materialization-report.json")
    assert not target.exists(), "already_admitted_no_replay"
    assert json.loads((OUT / "mixed-load.json").read_text())["status"] == "passed"
    with sqlite3.connect("file:" + str(ROOT / "var/autonomy/validation/state.sqlite") + "?mode=ro", uri=True) as db:
        used = db.execute("SELECT count(*) FROM validation_calls WHERE campaign='release-readiness-20261109-B'").fetchone()[0]
    assert used <= (36 if retest else 32)
    if retest:
        assert json.loads((OUT / "scope-fix/deployment-result.json").read_text())["status"] == "applied_behind_gate"
    experiment = json.loads((OUT / "natural-experiment.json").read_text())
    rows = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    cases = [("materialization", "ZYJ", 1, "请仅为批次scat20161015_20161021的100002准备阶段分段phase_segments物化方案，最多1架航班，不处理整批，不强制重算，不预测；先准备等待确认。"), ("report", "HYN", 5, "请复用本会话已经完成的实验任务" + experiment["task_id"] + "，单独准备一份汇总报告任务，仅引用这个任务的真实制品，不重新抽样、不重复计算模型或物化；先准备等待确认。")]
    report = {"status": "running", "cases": [], "new_gpu_calls": 0, "materialization_backend_already_real_tested_in_experiment": experiment["task_id"]}
    save(target, report)
    for case_id, user, index, question in cases:
        thread = next(r["thread_id"] for r in rows if r["user"] == user and r["index"] == index)
        with httpx.Client(base_url="http://127.0.0.1:18159/api/v1/hermes", timeout=45, trust_env=False, headers={"Origin": "https://172.26.67.148:8444", "X-Portal-Authenticated-User": user, "X-Portal-Gateway-Token": private_key(ROOT, "var/deploy/portal/keys/front.key")}) as http:
            path = "/threads/" + thread
            before = {t["task_id"] for t in http.get(path + "/model-tasks").json()["items"]}
            entry = {"id": case_id, "user": user, "thread_id": thread, "request_id": str(uuid4()), "status": "admission_intent", "question": question}
            report["cases"].append(entry)
            save(target, report)
            response = http.post(path + "/runs", json={"request_id": entry["request_id"], "message": question})
            response.raise_for_status()
            entry.update(run_id=response.json()["run_id"], status="planning")
            save(target, report)
            for _ in range(150):
                time.sleep(2)
                state = http.get(path + "/runs/" + entry["run_id"]).json()
                if state["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
            assert state["status"] == "completed"
            tasks = [t for t in http.get(path + "/model-tasks").json()["items"] if t["task_id"] not in before]
            assert len(tasks) == 1
            task = tasks[0]
            proposal = task["workflow"]["proposal"]
            assert proposal["kind"] == case_id
            if case_id == "materialization":
                assert proposal["external_ids"] == ["100002"] and proposal["max_flights"] == 1 and proposal["materializations"] == ["phase_segments"] and not proposal["models"]
            else:
                assert proposal["source_task_ids"] == [experiment["task_id"]] and not proposal["models"] and not proposal["materializations"]
            entry.update(status="planning_passed", task_id=task["task_id"], snapshot_sha256=task["snapshot_sha256"])
            save(OUT / "private" / ("remaining-plan-" + case_id + ".json"), {"state": state, "task": task})
            save(target, report)
            endpoint = path + "/model-tasks/" + task["task_id"]
            if case_id == "materialization":
                response = http.post(endpoint + "/revoke", json={})
                response.raise_for_status()
                entry["pending_plan_revoked_after_review"] = True
                entry["scope"] = "natural planning; actual bounded materialization already exercised in experiment"
            else:
                entry["status"] = "confirmation_intent"
                save(target, report)
                response = http.post(endpoint + "/confirm", json={"confirmed": True, "snapshot_sha256": task["snapshot_sha256"]})
                response.raise_for_status()
                for _ in range(180):
                    time.sleep(2)
                    task = http.get(endpoint).json()
                    if task["skill_job"]["state"] in {"succeeded", "failed", "paused", "unknown", "cancelled"}:
                        break
                save(OUT / "private/scoped-report-result.json", task)
                entry["status"] = "passed" if task["skill_job"]["state"] == "succeeded" else "gaps_found"
                entry["job_state"] = task["skill_job"]["state"]
                entry["artifacts"] = task.get("run", {}).get("result_artifact_ids", [])
            save(target, report)
    report["status"] = "passed_with_documented_scope" if all(c["status"] in {"planning_passed", "passed"} for c in report["cases"]) else "gaps_found"
    save(target, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
