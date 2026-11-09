"""Read-only audit of finite real query tests; saves metadata, never raw business data."""

import hashlib
import json
import re
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-query-display-rounds"


def main():
    records = json.loads((OUT / "natural-rounds.json").read_text())["records"]
    assert len(records) == 5 and len({r["run_id"] for r in records}) == 5
    container = json.loads(
        subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"])
    )[0]
    secret = dict(x.split("=", 1) for x in container["Config"]["Env"] if "=" in x)["AUTH_SECRET"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        req = urllib.request.Request(
            "http://127.0.0.1:18120/api/v1/hermes" + path,
            headers={"Authorization": "Bearer " + secret},
        )
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    cases = []
    with sqlite3.connect(
        "file:" + str(ROOT / "var/deploy/h7b/shared/homes/main/skill-loads.sqlite") + "?mode=ro",
        uri=True,
    ) as db:
        db.row_factory = sqlite3.Row
        for record in records:
            rid = record["run_id"]
            assert re.fullmatch(r"run_[A-Za-z0-9_-]+", rid)
            base = "/threads/" + record["thread_id"] + "/runs/" + rid
            state = get(base)
            refs = get(base + "/results")["items"]
            pages = []
            indices = set()
            totals = set()
            hashes_ok = True
            coordinates_ok = True
            for ref in refs:
                value = get(base + "/results/" + ref["result_id"])
                raw = value["result"]
                digest = hashlib.sha256(
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                hashes_ok = hashes_ok and digest == ref["payload_sha256"] == value["payload_sha256"]
                if ref["operation"] != "get_scat_track_points":
                    continue
                page = raw["data"]["pagination"]
                points = raw["data"]["points"]
                query = raw["query"]
                totals.add(page["total_points"])
                indices.update(query["start"] + i * query["every_nth"] for i in range(len(points)))
                analysis = value["analysis_result"]
                block = next(b for b in analysis["blocks"] if b["type"] == "trajectory_2d")
                coordinates_ok = (
                    coordinates_ok
                    and len(points) == len(block["points"])
                    and all(
                        a["position"]["lon_deg"] == b["longitude"]
                        and a["position"]["lat_deg"] == b["latitude"]
                        for a, b in zip(points, block["points"])
                    )
                )
                pages.append(
                    {
                        "start": query["start"],
                        "every_nth": query["every_nth"],
                        "points": len(points),
                        "has_more": page["has_more"],
                        "total": page["total_points"],
                    }
                )
            output = state.get("output", "")
            claim = re.search(r"读取了全部\s*(\d+)\s*个?点", output)
            budget = db.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (rid,),
            ).fetchone()
            loads = [
                dict(r)
                for r in db.execute(
                    "SELECT skill_id,version,file_path FROM skill_loads WHERE run_id=?", (rid,)
                )
            ]
            operations = [
                dict(r)
                for r in db.execute(
                    "SELECT operation,outcome,skill_id,skill_version FROM skill_operations WHERE run_id=?",
                    (rid,),
                )
            ]
            cases.append(
                {
                    "case": record["case"],
                    "run_id": rid,
                    "status": state["status"],
                    "answer_characters": len(output),
                    "receipts": len(refs),
                    "receipt_operations": [r["operation"] for r in refs],
                    "track_pages": pages,
                    "unique_loaded_track_points": len(indices),
                    "source_totals": sorted(totals),
                    "claimed_all_points": int(claim[1]) if claim else None,
                    "full_coverage_claim_wrong": bool(claim and int(claim[1]) != len(indices)),
                    "source_hashes_match": hashes_ok,
                    "analysis_coordinates_match": coordinates_ok,
                    "loads": loads,
                    "operations": operations,
                    "budget": dict(budget) if budget else None,
                    "guesses_plot_script": "plot_track.py" in output,
                    "claims_plot_unavailable": "无法绘制" in output,
                    "mentions_legacy_catalog": "scat_task_catalog" in output,
                    "mentioned_registered_models": [
                        n for n in ["constant-velocity-heading", "kalman-cv"] if n in output
                    ],
                }
            )
    report = {
        "cases": cases,
        "usage": {
            key: sum(c["budget"][key] or 0 for c in cases)
            for key in [
                "llm_calls",
                "tool_calls",
                "charged_tokens",
                "provider_input_tokens",
                "provider_output_tokens",
            ]
        },
        "new_prediction_tasks": 0,
        "new_questions": 5,
        "raw_trajectory_or_reply_saved": False,
        "workflow_catalog": get("/workflow-catalog"),
    }
    (OUT / "backend-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "cases": [
                    {
                        k: c[k]
                        for k in [
                            "case",
                            "status",
                            "receipts",
                            "unique_loaded_track_points",
                            "full_coverage_claim_wrong",
                            "loads",
                        ]
                    }
                    for c in cases
                ],
                "usage": report["usage"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
