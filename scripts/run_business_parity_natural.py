"""Six fresh ordinary-language checks; persistent admission markers prevent replay.

Existing per-run limits remain unchanged. Stop admitting after 1.5M measured
conservative units; an in-flight run remains bounded by its existing 2M limit.
No model task confirmation, GPU inference, publication or materialization.
"""

import getpass
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-business-parity"
CASES = [
    (
        "quality",
        0,
        "请检查批次scat20161015_20161021中航班100002最后200个观测点的输入数据质量，不运行预测，给出特征异常和修复明细。",
        "inspect_scat_model_data_quality",
    ),
    (
        "phase_followup",
        0,
        "再给出这个航班的飞行阶段统计，并用表格展示。",
        "get_scat_flight_phase_stats",
    ),
    (
        "uav_plot",
        1,
        "查找一架有起飞时间的正常UAV架次，读取它前20个位置样本并绘制二维轨迹图，保留单位和采样范围。",
        "get_uav_samples",
    ),
    (
        "uav_comparison",
        1,
        "再对比全部机型正常和异常样本的组合高度，只展示统计，不运行分类模型。",
        "get_uav_normal_vs_anomaly_stats",
    ),
    (
        "empty",
        2,
        "查询批次scat20161015_20161021中编号PARITY-NO-SUCH-FLIGHT的航班，不存在就说明查无数据，不要改用其他航班。",
        "search_scat_flights",
    ),
    (
        "empty_recovery",
        2,
        "改查同批次100002，给出已有空管预测的误差统计，不发起新的预测。",
        "get_scat_prediction_error_stats",
    ),
]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def budget(run):
    path = ROOT / "var/deploy/h7b/shared/homes/main/skill-loads.sqlite"
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT llm_calls,tool_calls,charged_tokens,exhausted,limits_json FROM skill_run_budgets WHERE run_id=?",
            (run,),
        ).fetchone()
    return dict(row) if row else {}


def main():
    os.umask(0o077)
    private = OUT / "private"
    private.mkdir(parents=True, exist_ok=True)
    record_file = OUT / "natural.json"
    if record_file.exists():
        raise ValueError("already_admitted_inspect_existing_runs_do_not_replay")
    report = {
        "status": "running",
        "maximum_new_questions": 6,
        "task_confirmations": 0,
        "admission_stop_units": 1500000,
        "per_run_limits_unchanged": True,
        "cases": [],
    }
    website_password = getpass.getpass("HYN website password: ")
    save(record_file, report)
    threads = {}
    with httpx.Client(
        base_url="https://172.26.67.148:8444/api/v1/hermes",
        auth=httpx.BasicAuth("HYN", website_password),
        headers={"Origin": "https://172.26.67.148:8444"},
        verify=False,  # Existing intranet certificate; website authentication is unchanged.
        timeout=30,
        trust_env=False,
    ) as http:
        for name, group, question, operation in CASES:
            if (
                sum(c.get("budget", {}).get("charged_tokens", 0) for c in report["cases"])
                >= 1500000
            ):
                report["status"] = "admission_budget_stopped"
                break
            if group not in threads:
                response = http.post("/threads", json={})
                response.raise_for_status()
                threads[group] = response.json()["thread_id"]
            thread = threads[group]
            entry = {
                "id": name,
                "thread_id": thread,
                "question": question,
                "request_id": str(uuid4()),
                "status": "admitted",
            }
            report["cases"].append(entry)
            save(record_file, report)  # Persist before the only POST admission.
            start = time.monotonic()
            response = http.post(
                "/threads/" + thread + "/runs",
                json={"request_id": entry["request_id"], "message": question},
            )
            entry["admission_http"] = response.status_code
            if response.status_code != 202:
                entry["status"] = "admission_failed_or_unknown"
                save(record_file, report)
                raise RuntimeError("admission_failed_no_retry")
            entry["run_id"] = response.json()["run_id"]
            entry["status"] = "running"
            save(record_file, report)
            endpoint = "/threads/" + thread + "/runs/" + entry["run_id"]
            for _ in range(120):
                time.sleep(2)
                response = http.get(endpoint)
                response.raise_for_status()
                state = response.json()
                if state["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
            else:
                http.post(endpoint + "/stop", json={})
                entry["status"] = "timed_out_stop_requested"
                save(record_file, report)
                raise RuntimeError("bounded_run_timeout")
            entry["seconds"] = round(time.monotonic() - start, 3)
            entry["run_status"] = state["status"]
            entry["budget"] = budget(entry["run_id"])
            refs_response = http.get(endpoint + "/results")
            refs_response.raise_for_status()
            refs = refs_response.json()["items"]
            receipts = []
            for ref in refs:
                response = http.get(endpoint + "/results/" + ref["result_id"])
                response.raise_for_status()
                receipts.append(response.json())
            save(private / (name + ".json"), {"state": state, "receipts": receipts})
            entry["operations"] = [r["operation"] for r in receipts]
            entry["references"] = [
                {k: r[k] for k in ["result_id", "operation", "payload_sha256"]} for r in receipts
            ]
            target = next((r["result"] for r in receipts if r["operation"] == operation), None)
            checks = {
                "completed": state["status"] == "completed",
                "expected_operation": target is not None,
            }
            if target:
                query = target.get("query", {})
                if name == "quality":
                    checks["source_limit_200"] = query.get("source_limit") == 200
                    checks["correct_flight"] = str(query.get("external_id")) == "100002"
                if name in {"phase_followup", "empty_recovery"}:
                    checks["correct_flight"] = str(query.get("external_id")) == "100002"
                    checks["correct_batch"] = query.get("batch") == "scat20161015_20161021"
                if name == "uav_plot":
                    checks["twenty_samples"] = len(target["data"].get("samples", [])) == 20
                    checks["known_absolute_origin"] = target["data"].get("start_time") is not None
                    analysis = next(r for r in receipts if r["operation"] == operation).get(
                        "analysis_result", {}
                    )
                    checks["spatial_and_temporal_blocks"] = {
                        "trajectory_2d",
                        "timeseries",
                    }.issubset({b["type"] for b in analysis.get("blocks", [])})
                if name == "uav_comparison":
                    checks["all_models"] = not query.get("model")
                    checks["correct_channel"] = query.get("channels") == ["cmp_comb_alt"]
                if name == "empty":
                    checks["no_fallback"] = query.get("external_id") == "PARITY-NO-SUCH-FLIGHT"
                    checks["empty_items"] = target["data"].get("items") == []
            entry["checks"] = checks
            entry["answer_sha256"] = hashlib.sha256(state.get("output", "").encode()).hexdigest()
            entry["status"] = "passed" if all(checks.values()) else "gaps_found"
            save(record_file, report)
            print(
                json.dumps(
                    {
                        "case": name,
                        "status": entry["status"],
                        "checks": checks,
                        "budget": entry["budget"],
                    }
                ),
                flush=True,
            )
        if report["status"] == "running":
            report["status"] = (
                "passed" if all(c["status"] == "passed" for c in report["cases"]) else "gaps_found"
            )
        save(record_file, report)


if __name__ == "__main__":
    main()
