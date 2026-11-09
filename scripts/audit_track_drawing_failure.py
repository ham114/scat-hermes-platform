"""Read-only audit of the user's failed track request; no LLM or new queries."""

import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "var/validation/20261107-track-drawing-failure"
THREAD = "3a298bec-bafd-48bd-b99c-509f295dbd1f"
RUN = "run_63a16cd882dd4b33bfd260959c1e12c8"


def read_db(path):
    db = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    container = json.loads(
        subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"], text=True)
    )[0]
    env = dict(x.split("=", 1) for x in container["Config"]["Env"] if "=" in x)
    token = env["AUTH_SECRET"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        req = urllib.request.Request(
            "http://127.0.0.1:18120/api/v1/hermes" + path,
            headers={"Authorization": "Bearer " + token},
        )
        return json.load(opener.open(req, timeout=30))

    base = f"/threads/{THREAD}/runs/{RUN}"
    state = get(base)
    report = {
        "run_id": RUN,
        "thread_id": THREAD,
        "status": state["status"],
        "provider_output_truncated": "output length limit" in (state.get("output") or ""),
        "active_release": (ROOT / "var/deploy/autonomy/active-release").read_text().strip(),
        "llm_calls": 0,
        "new_database_queries": 0,
        "prediction_requests": 0,
    }
    home = ROOT / "var/deploy/h7b/shared/homes/main"
    with read_db(home / "skill-loads.sqlite") as db:
        loads = [
            dict(r)
            for r in db.execute(
                "SELECT session_id,skill_id,version,file_path FROM skill_loads WHERE run_id=?",
                (RUN,),
            )
        ]
        report["loads"] = [{k: v for k, v in row.items() if k != "session_id"} for row in loads]
        budget = db.execute(
            "SELECT llm_calls,tool_calls,charged_tokens,exhausted FROM skill_run_budgets WHERE run_id=?",
            (RUN,),
        ).fetchone()
        report["failed_run_usage"] = dict(budget) if budget else None
    report["calls"] = []
    with read_db(home / "state.db") as db:
        for session in {row["session_id"] for row in loads}:
            for row in db.execute(
                "SELECT role,tool_name,tool_calls,content FROM messages WHERE session_id=? AND timestamp BETWEEN ? AND ? ORDER BY id",
                (session, 1794007440, 1794007545),
            ):
                if row["tool_calls"]:
                    for call in json.loads(row["tool_calls"]):
                        fn = call.get("function", call)
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        report["calls"].append(
                            {
                                "tool": fn.get("name"),
                                "argument_keys": sorted(args),
                                "page": {
                                    k: args[k]
                                    for k in [
                                        "start",
                                        "limit",
                                        "every_nth",
                                        "fields",
                                        "name",
                                        "file_path",
                                    ]
                                    if k in args
                                },
                            }
                        )
                if row["role"] == "assistant" and row["content"]:
                    text = row["content"]
                    report.setdefault("assistant_content_shapes", []).append(
                        {
                            "bytes": len(text.encode()),
                            "mentions_svg": bool(re.search(r"<svg|svg|SVG", text)),
                            "mentions_plot_script": bool(
                                re.search(r"plot_track|matplotlib|plt\.", text)
                            ),
                        }
                    )
    report["receipts"] = []
    from application.readonly_display import receipt_display

    for ref in get(base + "/results")["items"]:
        item = get(base + "/results/" + ref["result_id"])
        raw = item["result"]
        data = raw.get("data") or {}
        detail = {
            "operation": ref["operation"],
            "result_id": ref["result_id"],
            "deployed_has_analysis": "analysis_result" in item,
            "representation": raw.get("representation"),
            "pagination": data.get("pagination"),
            "point_count": len(data.get("points", [])),
        }
        if ref["operation"] == "get_scat_track_points":
            projected = receipt_display(item)
            detail["candidate_can_render"] = "analysis_result" in projected
            if detail["candidate_can_render"]:
                detail["block_types"] = sorted(
                    {b["type"] for b in projected["analysis_result"]["blocks"]}
                )
        report["receipts"].append(detail)
    files = [
        "src/service/hermes_api.py",
        "src/application/readonly_display.py",
        "scripts/run_hermes_api.py",
    ]
    probe = (
        "import pathlib,hashlib,json; files="
        + repr(files)
        + ";print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() if pathlib.Path(p).exists() else None for p in files}))"
    )
    report["running_sources"] = json.loads(
        subprocess.check_output(
            [
                "docker",
                "exec",
                "scat-hermes-h7b-platform-1",
                "/opt/conda/envs/scat-agent/bin/python",
                "-B",
                "-c",
                probe,
            ],
            text=True,
        )
    )
    (OUT / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
