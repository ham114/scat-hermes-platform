"""Finite new ordinary-language checks under the frozen aggregate validation cap."""

import hashlib
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

CASES = [
    (
        "problem_first",
        0,
        "请列出批次scat20161015_20161021中的问题航班，先显示第一页2条，保留问题类型和分页范围，不运行模型。",
        "list_scat_quality_problem_flights",
        {"limit": 2, "offset": 0},
    ),
    (
        "problem_next",
        0,
        "请继续下一页，仍然只显示2条问题航班。",
        "list_scat_quality_problem_flights",
        {"limit": 2, "offset": 2},
    ),
    (
        "clean_first",
        1,
        "请列出批次scat20161015_20161021中按当前质量规则合格的航班，先显示第一页2条，说明质量规则边界，不做物化或预测。",
        "list_scat_quality_clean_flights",
        {"limit": 2, "offset": 0},
    ),
    (
        "clean_next",
        1,
        "再看下一页，仍然2条合格航班。",
        "list_scat_quality_clean_flights",
        {"limit": 2, "offset": 2},
    ),
    (
        "empty",
        2,
        "查询批次scat20161015_20161021中编号READINESS-NO-SUCH-FLIGHT的航班。不存在就明确查无数据，不要改查其他航班。",
        "search_scat_flights",
        {"external_id": "READINESS-NO-SUCH-FLIGHT"},
    ),
    (
        "empty_recovery",
        2,
        "那就改查同批次100002，展示已有空管预测的误差统计，不生成新预测。",
        "get_scat_prediction_error_stats",
        {"external_id": "100002"},
    ),
    (
        "cfpl_first",
        3,
        "请读取批次scat20161015_20161021整批已有物化的CFPL计划与实飞误差汇总，overall分组、全部冻结预测时域，每页2条，先第一页。保留样本和航班覆盖，缺失就说缺失，不运行新模型或刷新物化。",
        "get_prediction_metric_summaries",
        {"group_by": "overall", "limit": 2, "offset": 0},
    ),
    (
        "cfpl_next",
        3,
        "继续看下一页，仍然2条相同口径的CFPL汇总。",
        "get_prediction_metric_summaries",
        {"group_by": "overall", "limit": 2, "offset": 2},
    ),
]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def budget():
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/validation/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        row = db.execute(
            "SELECT count(*),coalesce(sum(units),0) FROM validation_calls WHERE campaign=?",
            ("release-readiness-20261109-A",),
        ).fetchone()
    return {
        "reserved_calls": row[0],
        "reserved_units": row[1],
        "max_calls": 96,
        "max_units": 8000000,
    }


def main(cases=None, suite="business"):
    os.umask(0o077)
    private = OUT / "private"
    private.mkdir(exist_ok=True)
    record = OUT / ("natural-" + suite + ".json")
    assert not record.exists(), "already_admitted_inspect_do_not_replay"
    threads = json.loads((private / "native-campaign-threads.json").read_text())
    plan = [
        {"id": n, "thread_index": i, "question": q, "operation": op, "expected_query": args}
        for n, i, q, op, args in (cases if cases is not None else CASES)
    ]
    plan_file = OUT / ("natural-" + suite + "-plan.json")
    assert not plan_file.exists()
    save(plan_file, plan)
    report = {
        "status": "running",
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "task_confirmations": 0,
        "new_gpu_calls": 0,
        "cases": [],
    }
    save(record, report)
    key = private_key(ROOT, "var/deploy/portal/keys/front.key")
    with httpx.Client(
        base_url="http://127.0.0.1:18159/api/v1/hermes",
        trust_env=False,
        timeout=30,
        headers={
            "Origin": "https://172.26.67.148:8444",
            "X-Portal-Authenticated-User": "HYN",
            "X-Portal-Gateway-Token": key,
        },
    ) as http:
        for case in plan:
            thread = next(
                t["thread_id"]
                for t in threads
                if t["user"] == "HYN" and t["index"] == case["thread_index"]
            )
            entry = {
                "id": case["id"],
                "thread_id": thread,
                "request_id": str(uuid4()),
                "status": "admission_pending",
            }
            report["cases"].append(entry)
            save(record, report)
            started = time.monotonic()
            try:
                response = http.post(
                    "/threads/" + thread + "/runs",
                    json={"request_id": entry["request_id"], "message": case["question"]},
                )
            except httpx.HTTPError:
                entry["status"] = "admission_unknown_no_replay"
                report["status"] = "stopped_unknown"
                save(record, report)
                raise
            entry["http"] = response.status_code
            if response.status_code != 202:
                entry["status"] = "admission_rejected"
                report["status"] = "stopped"
                save(record, report)
                break
            entry["run_id"] = response.json()["run_id"]
            entry["status"] = "running"
            save(record, report)
            endpoint = "/threads/" + thread + "/runs/" + entry["run_id"]
            for _ in range(300):
                time.sleep(2)
                response = http.get(endpoint)
                response.raise_for_status()
                state = response.json()
                if state["status"] in ["completed", "failed", "cancelled", "interrupted"]:
                    break
            else:
                http.post(endpoint + "/stop", json={})
                entry["status"] = "timeout_stop_requested"
                report["status"] = "stopped"
                save(record, report)
                break
            refs = http.get(endpoint + "/results")
            refs.raise_for_status()
            receipts = []
            for ref in refs.json()["items"]:
                response = http.get(endpoint + "/results/" + ref["result_id"])
                response.raise_for_status()
                receipts.append(response.json())
            save(
                private / ("natural-" + case["id"] + ".json"),
                {"state": state, "receipts": receipts},
            )
            matched = [
                r
                for r in receipts
                if r["operation"] == case["operation"] and r["result"].get("status") == "ok"
            ]
            query = matched[-1]["result"].get("query", {}) if matched else {}
            checks = {
                "completed": state["status"] == "completed",
                "expected_receipt": bool(matched),
                "same_batch": query.get("batch") == "scat20161015_20161021",
                **{"query_" + k: query.get(k) == v for k, v in case["expected_query"].items()},
            }
            if case["id"] == "empty":
                checks["no_fallback"] = (
                    bool(matched) and matched[-1]["result"]["data"]["items"] == []
                )
            entry.update(
                status="passed" if all(checks.values()) else "gaps_found",
                checks=checks,
                operations=[r["operation"] for r in receipts],
                source_sha256=[r["payload_sha256"] for r in receipts],
                seconds=round(time.monotonic() - started, 3),
                budget=budget(),
                answer_sha256=hashlib.sha256(state.get("output", "").encode()).hexdigest(),
            )
            save(record, report)
            print(
                json.dumps(
                    {
                        "id": entry["id"],
                        "status": entry["status"],
                        "checks": checks,
                        "budget": entry["budget"],
                    }
                ),
                flush=True,
            )
            if state["status"] != "completed":
                report["status"] = "stopped_failed"
                break
        if report["status"] == "running":
            report["status"] = (
                "passed"
                if all(r["status"] == "passed" for r in report["cases"])
                and len(report["cases"]) == len(plan)
                else "gaps_found"
            )
        report["budget"] = budget()
        save(record, report)


if __name__ == "__main__":
    main()
