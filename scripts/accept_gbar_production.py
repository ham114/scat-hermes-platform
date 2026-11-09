"""Two bounded production cases; each mutation is recorded before its single HTTP send."""

import argparse
import json
import sqlite3
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"
LIMITS = {"max_turns": 24, "max_llm_tokens": 1000000, "max_tool_calls": 80, "wall_seconds": 1800}


def token():
    row = json.loads(subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"]))[
        0
    ]
    return next(x.split("=", 1)[1] for x in row["Config"]["Env"] if x.startswith("AUTH_SECRET="))


def api(path, body=None, method=None):
    request = urllib.request.Request(
        "http://127.0.0.1:18120/api/v1/hermes" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        request, timeout=45
    ) as response:
        return json.load(response)


def save(case, record):
    (OUT / (case + ".json")).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def receipts():
    with sqlite3.connect(
        "file:" + str(ROOT / "var/gpu/requests.sqlite") + "?mode=ro", uri=True
    ) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in db.execute(
                "SELECT id,task,state,calls,request_sha,output_sha FROM gpu_requests"
            )
        ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "inspect", "confirm", "verify"])
    parser.add_argument("case", choices=["forecast", "backtest"])
    args = parser.parse_args()
    file = OUT / (args.case + ".json")
    if args.action == "start":
        record = {
            "status": "admitted",
            "case": args.case,
            "driver": "natural_main_and_native_worker"
            if args.case == "forecast"
            else "rest_prepare_and_native_worker",
            "gpu_before": receipts(),
            "budget": LIMITS,
            "confirmations": 0,
        }
        assert not any(r["state"] in {"running", "unknown"} for r in record["gpu_before"])
        with file.open("x") as stream:
            stream.write(json.dumps(record, indent=2))
        thread = api("/threads", {})["thread_id"]
        record["thread_id"] = thread
        record["status"] = "send_intent"
        save(args.case, record)
        if args.case == "forecast":
            response = api(
                "/threads/" + thread + "/runs",
                {
                    "request_id": str(uuid4()),
                    "message": "取批次scat20161015_20161021中航班100002的最后200个观测点，用GBAR从最后一个观测点向未来预测10、30、60秒。只处理这一架航班、一个模型和一个预测起点，观测窗口须符合GBAR要求。请准备方案等待确认，没有未来真值就不要计算误差。",
                },
            )
            record.update(planning_run_id=response["run_id"], status="planning")
        else:
            first = json.loads((OUT / "forecast.json").read_text())
            assert first["status"] == "passed"
            response = api(
                "/threads/" + thread + "/workflow-tasks",
                {
                    "request_id": str(uuid4()),
                    "budget": LIMITS,
                    "proposal": {
                        "kind": "comparison",
                        "batch": "scat20161015_20161021",
                        "external_ids": ["100002"],
                        "max_flights": 1,
                        "models": [{"model": "scat-gbar-v23", "version": "23"}],
                        "min_observed_s": 350,
                        "max_cutoffs_per_flight": 1,
                        "horizons_s": [10, 30, 60],
                        "forecast_origin": "historical_cutoffs",
                    },
                },
            )
            record.update(
                task_id=response["task_id"], execution_run_id=response["run_id"], status="pending"
            )
        save(args.case, record)
        print(json.dumps({k: v for k, v in record.items() if k != "gpu_before"}))
        return
    record = json.loads(file.read_text())
    base = "/threads/" + record["thread_id"]
    if "task_id" not in record:
        state = api(base + "/runs/" + record["planning_run_id"])
        record["planning_status"] = state["status"]
        if state["status"] == "completed":
            tasks = api(base + "/model-tasks")["items"]
            assert len(tasks) == 1
            record.update(
                task_id=tasks[0]["task_id"], execution_run_id=tasks[0]["run_id"], status="pending"
            )
        save(args.case, record)
        if "task_id" not in record:
            print(json.dumps({"planning_status": state["status"], "case": args.case}))
            return
    endpoint = base + "/model-tasks/" + record["task_id"]
    task = api(endpoint)
    p = task["workflow"]["proposal"]
    assert p["external_ids"] == ["100002"] and p["horizons_s"] == [10, 30, 60]
    assert (
        len(p["models"]) == 1
        and p["models"][0]["model"] == "scat-gbar-v23"
        and p["models"][0]["version"] == "23"
    )
    assert p["repeats"] == 1 and task["workflow"]["coverage"]["selected_count"] == 1
    assert task["workflow"]["gpu"]["device"] == "cuda"
    if args.case == "forecast":
        assert (
            p["kind"] == "forecast"
            and p["forecast_origin"] == "last_observation"
            and p["observation_last_n"] == 200
        )
        windows = task["workflow"]["prediction_contract"]["windows"]
        assert len(windows) == 1 and windows[0]["point_count"] == 200
    else:
        assert p["kind"] == "comparison" and p["max_cutoffs_per_flight"] == 1
    if args.action == "confirm":
        assert task["state"] == "pending" and record["confirmations"] == 0
        assert len(receipts()) == len(record["gpu_before"]), "Unconfirmed task cannot compute"
        assert all(task["skill_job"]["budget"][k] <= v for k, v in LIMITS.items())
        record.update(
            confirmations=1,
            status="confirmation_send_intent",
            approved_snapshot=task["snapshot_sha256"],
        )
        save(args.case, record)
        api(endpoint + "/confirm", {"snapshot_sha256": task["snapshot_sha256"], "confirmed": True})
        record["status"] = "executing"
    job = task["skill_job"]
    if args.action == "verify":
        assert job["state"] == "succeeded", job["state"]
        assert len(job["backend_executions"]) == 1 and job["backend_executions"][0]["calls"] == 1
        ids = task["run"]["result_artifact_ids"]
        assert len(ids) == 2
        hashes = []
        pages = []
        for identity in ids:
            body = api(endpoint + "/artifacts/" + identity)
            hashes.append(body["payload_sha256"])
            result = body["analysis_result"]
            pages.append(
                {
                    "artifact_id": identity,
                    "operation": result["operation"],
                    "context": result["context"],
                    "block_types": [b["type"] for b in result["blocks"]],
                }
            )
            if args.case == "forecast":
                assert result["context"]["prediction_mode"] == "future_forecast"
                assert not any(
                    c["key"].startswith(("mae_", "rmse_", "ade_", "fde_"))
                    for b in result["blocks"]
                    for c in b.get("columns", [])
                )
        for identity, sha in zip(ids, hashes, strict=True):
            assert api(endpoint + "/artifacts/" + identity)["payload_sha256"] == sha
        other = api("/threads", {})["thread_id"]
        try:
            api(endpoint.replace(record["thread_id"], other) + "/artifacts/" + ids[0])
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Cross-thread access allowed")
        before_ids = {r["id"] for r in record["gpu_before"]}
        new = [r for r in receipts() if r["id"] not in before_ids]
        assert len(new) == 1 and new[0]["state"] == "succeeded" and new[0]["calls"] == 1
        assert new[0]["task"] == record["task_id"]
        assert job["budget"]["llm_calls"] <= 24 and job["budget"]["charged_tokens"] <= 1000000
        record.update(
            status="passed",
            artifact_ids=ids,
            artifact_hashes=hashes,
            artifact_pages=pages,
            gpu_receipts=new,
            budget_usage=job["budget"],
            backend_executions=job["backend_executions"],
            cross_thread_rejected=True,
            repeated_get_without_recompute=True,
        )
    save(args.case, record)
    print(
        json.dumps(
            {
                "case": args.case,
                "status": record["status"],
                "job_state": job["state"],
                "thread_id": record["thread_id"],
                "task_id": record["task_id"],
                "budget": job["budget"],
                "backend": job.get("backend_executions", []),
            }
        )
    )


if __name__ == "__main__":
    main()
