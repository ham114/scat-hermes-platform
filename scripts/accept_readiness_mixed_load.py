"""Three signed portal users, two real queued GPU workflows and one ordinary readonly run."""
import concurrent.futures
import json
import os
import sqlite3
import subprocess
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


def gpu_rows():
    with sqlite3.connect("file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT id,task,state,calls,created,finished,output_sha FROM gpu_requests")]


def main():
    os.umask(0o077)
    record = OUT / "mixed-load.json"
    assert not record.exists(), "already_admitted_no_replay"
    assert json.loads((OUT / "gpu-conda-production-result.json").read_text())["status"] == "applied_behind_gate"
    rows = json.loads((OUT / "private/native-campaign-threads.json").read_text())
    key = private_key(ROOT, "var/deploy/portal/keys/front.key")
    clients = {u: httpx.Client(base_url="http://127.0.0.1:18159/api/v1/hermes", timeout=45, trust_env=False, headers={"Origin": "https://172.26.67.148:8444", "X-Portal-Authenticated-User": u, "X-Portal-Gateway-Token": key}) for u in ["HYN", "ZYJ", "admin"]}
    threads = {u: next(r["thread_id"] for r in rows if r["user"] == u and r["index"] == 0) for u in clients}
    before = gpu_rows()
    assert not any(r["state"] in {"running", "unknown"} for r in before)
    report = {"status": "running", "identity_evidence": "three live trusted gateway principals; only HYN browser password separately tested", "gpu_calls_before": len(before), "max_new_gpu_calls": 2, "jobs": [], "samples": [], "model_confirmations": 0}
    save(record, report)
    worker_stopped = False
    try:
        for user in ["HYN", "ZYJ"]:
            client, thread = clients[user], threads[user]
            entry = {"user": user, "thread_id": thread, "request_id": str(uuid4()), "status": "prepare_intent"}
            report["jobs"].append(entry)
            save(record, report)
            proposal = {"kind": "forecast", "batch": "scat20161015_20161021", "external_ids": ["100002"], "max_flights": 1, "models": [{"model": "scat-gbar-v23", "version": "23"}], "observation_last_n": 200, "forecast_origin": "last_observation", "min_observed_s": 350, "max_cutoffs_per_flight": 1, "horizons_s": [10, 30, 60]}
            response = client.post(f"/threads/{thread}/workflow-tasks", json={"request_id": entry["request_id"], "proposal": proposal, "budget": {"max_turns": 16, "max_llm_tokens": 1000000, "max_tool_calls": 60, "wall_seconds": 900}})
            response.raise_for_status()
            task = response.json()
            assert task["workflow"]["prediction_contract"]["windows"][0]["point_count"] == 200
            entry.update(task_id=task["task_id"], snapshot_sha256=task["snapshot_sha256"], status="pending")
            save(record, report)
        subprocess.run(["docker", "stop", "--time=30", "scat-hermes-h7b-worker-1"], capture_output=True, check=True, timeout=45)
        worker_stopped = True
        for entry in report["jobs"]:
            endpoint = f"/threads/{entry['thread_id']}/model-tasks/{entry['task_id']}"
            entry.update(status="confirmation_intent", confirmed_at=time.time())
            save(record, report)
            response = clients[entry["user"]].post(endpoint + "/confirm", json={"confirmed": True, "snapshot_sha256": entry["snapshot_sha256"]})
            response.raise_for_status()
            report["model_confirmations"] += 1
        queued = []
        for entry in report["jobs"]:
            task = clients[entry["user"]].get(f"/threads/{entry['thread_id']}/model-tasks/{entry['task_id']}").json()
            queued.append(task["skill_job"]["state"])
        assert queued == ["queued", "queued"]
        report["both_durably_queued_before_worker_start"] = True
        admin = {"thread_id": threads["admin"], "request_id": str(uuid4()), "status": "admission_intent"}
        report["readonly"] = admin
        save(record, report)
        response = clients["admin"].post(f"/threads/{threads['admin']}/runs", json={"request_id": admin["request_id"], "message": "请读取SCAT数据库概览，说明航班数量和批次数，不读取全量轨迹，不做物化或模型预测。"})
        response.raise_for_status()
        admin.update(run_id=response.json()["run_id"], admitted_at=time.time())
        subprocess.run(["docker", "start", "scat-hermes-h7b-worker-1"], capture_output=True, check=True, timeout=30)
        worker_stopped = False
        started = time.monotonic()

        def reads(user):
            samples = []
            with httpx.Client(base_url="http://127.0.0.1:18159/api/v1/hermes", timeout=30, trust_env=False, headers=dict(clients[user].headers)) as client:
                for _ in range(12):
                    begin = time.monotonic()
                    response = client.get("/threads?limit=2")
                    samples.append({"status": response.status_code, "seconds": round(time.monotonic() - begin, 4)})
                    time.sleep(0.15)
            return {"user": user, "samples": samples}

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            report["parallel_reads"] = list(executor.map(reads, list(clients)))
        for _ in range(420):
            statuses = []
            for entry in report["jobs"]:
                response = clients[entry["user"]].get(f"/threads/{entry['thread_id']}/model-tasks/{entry['task_id']}")
                response.raise_for_status()
                task = response.json()
                state = task["skill_job"]["state"]
                statuses.append(state)
                entry["status"] = state
                if state == "succeeded" and "finished_at" not in entry:
                    entry["finished_at"] = time.time()
                    save(OUT / "private" / ("mixed-task-" + entry["user"] + ".json"), task)
            response = clients["admin"].get(f"/threads/{threads['admin']}/runs/{admin['run_id']}")
            response.raise_for_status()
            admin["status"] = response.json()["status"]
            report["samples"].append({"seconds": round(time.monotonic() - started, 3), "gpu_job_states": statuses, "readonly_state": admin["status"]})
            save(record, report)
            if all(s in {"succeeded", "failed", "cancelled", "unknown", "paused", "partial"} for s in statuses) and admin["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(2)
        after = [r for r in gpu_rows() if r["id"] not in {r["id"] for r in before}]
        report["new_gpu_receipts"] = after
        checks = {"both_succeeded": all(j["status"] == "succeeded" for j in report["jobs"]), "readonly_completed": admin["status"] == "completed", "two_single_send_receipts": len(after) == 2 and all(r["state"] == "succeeded" and r["calls"] == 1 for r in after), "parallel_reads_all_200": all(s["status"] == 200 for r in report["parallel_reads"] for s in r["samples"])}
        ordered = sorted(after, key=lambda r: r["created"])
        checks["gpu_execution_nonoverlap"] = len(ordered) == 2 and ordered[0]["finished"] <= ordered[1]["created"]
        for entry in report["jobs"]:
            other = clients["ZYJ" if entry["user"] == "HYN" else "HYN"]
            checks[entry["user"] + "_cross_owner_404"] = other.get(f"/threads/{entry['thread_id']}/model-tasks/{entry['task_id']}/saved-model-output").status_code == 404
        report.update(status="passed" if all(checks.values()) else "gaps_found", checks=checks, elapsed_seconds=round(time.monotonic() - started, 3))
        save(record, report)
        print(json.dumps({k: v for k, v in report.items() if k not in {"samples", "parallel_reads"}}, ensure_ascii=False))
    finally:
        if worker_stopped:
            subprocess.run(["docker", "start", "scat-hermes-h7b-worker-1"], capture_output=True, check=True, timeout=30)
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    main()
