"""Summarize the one post-cutover browser run without exporting trajectory or reply text."""

import json
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-track-drawing-failure"


def main():
    live = json.loads((OUT / "live-run.json").read_text())
    assert live["status"] == "passed"
    row = json.loads(
        subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"], text=True)
    )[0]
    token = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x)["AUTH_SECRET"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        request = urllib.request.Request(
            "http://127.0.0.1:18120/api/v1/hermes" + path,
            headers={"Authorization": "Bearer " + token},
        )
        return json.load(opener.open(request, timeout=30))

    base = "/threads/" + live["thread_id"] + "/runs/" + live["run_id"]
    state = get(base)
    assert state["status"] == "completed" and state["completed"] and not state.get("partial")
    output = state.get("output", "")
    assert len(output) < 1200
    points = []
    for ref in get(base + "/results")["items"]:
        if ref["operation"] != "get_scat_track_points":
            continue
        payload = get(base + "/results/" + ref["result_id"])
        trajectory = next(
            b for b in payload["analysis_result"]["blocks"] if b["type"] == "trajectory_2d"
        )
        page = payload["result"]["data"]["pagination"]
        points.append(
            {
                "points": len(trajectory["points"]),
                "has_more": page["has_more"],
                "total": page["total_points"],
                "source_hash_matches": payload["payload_sha256"] == ref["payload_sha256"],
            }
        )
    assert len(points) == 1
    trace = ROOT / "var/deploy/h7b/shared/homes/main/skill-loads.sqlite"
    with sqlite3.connect("file:" + trace.as_posix() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        budget = dict(
            db.execute(
                "SELECT llm_calls,tool_calls,charged_tokens,provider_input_tokens,provider_output_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
                (live["run_id"],),
            ).fetchone()
        )
        loads = [
            dict(r)
            for r in db.execute(
                "SELECT skill_id,version,file_path FROM skill_loads WHERE run_id=?",
                (live["run_id"],),
            )
        ]
        operations = [
            r[0]
            for r in db.execute(
                "SELECT operation FROM skill_operations WHERE run_id=?", (live["run_id"],)
            )
        ]
    assert budget["exhausted"] is None
    assert operations.count("mcp__scat__get_scat_track_points") == 1
    assert not any("prepare" in op or "trial_run" in op or "model_run" in op for op in operations)
    report = {
        "status": "passed",
        "thread_id": live["thread_id"],
        "run_id": live["run_id"],
        "output_characters": len(output),
        "result_pages": points,
        "usage": budget,
        "skill_loads": loads,
        "operations": operations,
        "browser_posts": live["posts"],
        "browser_authorization_headers": live["auth"],
        "new_prediction_requests": 0,
        "raw_trajectory_or_reply_saved": False,
    }
    (OUT / "live-run-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
