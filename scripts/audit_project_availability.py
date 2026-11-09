"""Read-only availability audit; no model requests, task submission, or service changes."""

import hashlib
import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-project-availability"
RUNS = [
    "run_df70ec550dfa4154990ca20f2171d535",
    "run_12e4c9ea2ff04300800a52321e6bef4c",
    "run_fc1e30e785ed423c8d34593b9e9e4beb",
    "run_d719e5b251b54df3b3b8986cd1f77e23",
]
FILES = [
    "src/service/hermes_app.py",
    "src/service/memory_api.py",
    "src/service/skill_authoring_api.py",
    "src/hermes_runtime/agent_settings.py",
    "src/workflow_runtime/catalog.py",
    "src/workflow_runtime/contracts.py",
    "src/hermes_runtime/results.py",
    "src/hermes_runtime/skill_guard.py",
    "src/hermes_runtime/workflow_bridge.py",
]
FLAGS = {
    "SCAT_GPU_CONFIG_FILE", "SCAT_GBAR_V23_ENABLED", "SCAT_S5_WORKFLOWS",
    "SCAT_SKILL_JOBS", "SCAT_S5_SYNTHETIC", "SCAT_AUTONOMY_CONFIG",
    "SCAT_RAG_BASELINE_ENABLED", "SCAT_RAG_BASELINE_URL", "HERMES_HOME",
}


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=30)


def database(path):
    db = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as response:
            return {"http": response.status, "body": json.load(response)}
    except urllib.error.HTTPError as exc:
        return {"http": exc.code}
    except (OSError, ValueError) as exc:
        return {"error": type(exc).__name__}


def main():
    report = {"date": "2026-11-07", "audit_mode": "read_only_no_model_or_compute"}
    report["release"] = (ROOT / "var/deploy/autonomy/active-release").read_text().strip()
    report["containers"] = {}
    token = None
    for service in ["platform", "actions", "worker", "hermes-main", "hermes-worker", "author-api", "hermes-author"]:
        name = "scat-hermes-h7b-" + service + "-1"
        row = json.loads(docker("inspect", name))[0]
        env = dict(item.split("=", 1) for item in row["Config"]["Env"] if "=" in item)
        if service == "platform":
            token = env.get("AUTH_SECRET")
        report["containers"][service] = {
            "image": row["Image"], "health": row["State"].get("Health", {}).get("Status"),
            "flags": {k: v for k, v in env.items() if k in FLAGS},
        }
    assert token, "Running platform authentication unavailable"
    report["catalogs"] = {
        name: get("http://127.0.0.1:18120/api/v1/hermes/" + name, token)
        for name in ["model-catalog", "workflow-catalog"]
    }
    for name, port in [("platform", 18120), ("author", 18124)]:
        value = get("http://127.0.0.1:" + str(port) + "/openapi.json")
        body = value.pop("body", {})
        value["paths"] = {p: sorted(v) for p, v in body.get("paths", {}).items()}
        report[name + "_openapi"] = value
    # Compare actual running image sources, rather than assuming an uploaded tree is deployed.
    probe = "import pathlib,hashlib,json; r=pathlib.Path.cwd(); files=" + repr(FILES) + "; print(json.dumps({p:hashlib.sha256((r/p).read_bytes()).hexdigest() if (r/p).is_file() else None for p in files}))"
    report["source_vs_running"] = {}
    for role in ["platform", "author-api"]:
        name = "scat-hermes-h7b-" + role + "-1"
        current = json.loads(docker("exec", name, "/opt/conda/envs/scat-agent/bin/python", "-B", "-c", probe))
        report["source_vs_running"][role] = {
            p: {"source_sha256": hashlib.sha256((ROOT / p).read_bytes()).hexdigest(),
                "running_sha256": current[p], "same": current[p] == hashlib.sha256((ROOT / p).read_bytes()).hexdigest()}
            for p in FILES
        }
    report["traces"] = {}
    home = ROOT / "var/deploy/h7b/shared/homes/main"
    for filename in ["skill-loads.sqlite", "state.db"]:
        path = home / filename
        if not path.exists():
            continue
        with database(path) as db:
            schemas = {row[0]: row[1] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
            result = {"schemas": schemas}
            if filename == "skill-loads.sqlite":
                for table in ["skill_loads", "skill_operations"]:
                    if table in schemas:
                        fields = "run_id,skill_id,version,file_path" if table == "skill_loads" else "run_id,operation,skill_id,skill_version,outcome"
                        result[table] = [dict(row) for row in db.execute("SELECT " + fields + " FROM " + table + " WHERE run_id IN (?,?,?,?) ORDER BY id", RUNS)]
            report["traces"][filename] = result
    report["native_session_files"] = [p.relative_to(home).as_posix() for p in (home / "sessions").glob("*")][:20]
    with database(home / "skill-loads.sqlite") as trace, database(home / "state.db") as native:
        session_ids = [row[0] for row in trace.execute("SELECT DISTINCT session_id FROM skill_loads WHERE run_id IN (?,?,?,?)", RUNS)]
        report["conversation_tool_trace"] = []
        for session_id in session_ids:
            for row in native.execute("SELECT role,tool_name,tool_calls,content,timestamp FROM messages WHERE session_id=? AND timestamp BETWEEN ? AND ? ORDER BY id", (session_id, 1794003400, 1794003800)):
                item = {"role": row["role"], "tool": row["tool_name"], "timestamp": row["timestamp"]}
                if row["tool_calls"]:
                    calls = json.loads(row["tool_calls"])
                    item["calls"] = []
                    for call in calls:
                        function = call.get("function", call)
                        arguments = function.get("arguments", {})
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except ValueError:
                                arguments = {}
                        item["calls"].append({"name": function.get("name"), "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [], "skill_file": {k: arguments[k] for k in ["name", "skill_name", "path", "file_path", "script"] if isinstance(arguments, dict) and k in arguments}})
                if row["role"] == "tool":
                    content = row["content"] or ""
                    item["known_failure_codes"] = sorted(set(re.findall(r"skill_operation_not_allowed|skill_file_not_published|script_not_published|skill_required", content)))
                    item["content_bytes"] = len(content.encode())
                if row["role"] == "tool" or row["tool_calls"]:
                    report["conversation_tool_trace"].append(item)
    report["author_route_presence"] = {}
    probe = "import ast,pathlib,json; t=ast.parse(pathlib.Path('src/service/skill_authoring_api.py').read_text()); print(json.dumps([{'method':d.func.attr,'path':d.args[0].value} for n in ast.walk(t) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) for d in n.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr in ['get','post','put','delete','patch'] and d.args and isinstance(d.args[0],ast.Constant)]))"
    report["author_route_presence"]["running_decorators"] = json.loads(docker("exec", "scat-hermes-h7b-author-api-1", "/opt/conda/envs/scat-agent/bin/python", "-B", "-c", probe))
    with database(ROOT / "var/deploy/h7b/shared/skills/catalog.sqlite") as db:
        active = dict(db.execute("SELECT sha,generation FROM active WHERE id=1").fetchone())
    bundle = ROOT / "var/deploy/h7b/shared/skills/bundles" / active["sha"]
    report["active_skills"] = {"sha": active["sha"], "generation": active["generation"], "manifest": json.loads((bundle / "lock.json").read_text())}
    config = ROOT / "var/gpu/production.json"
    report["gpu_registration"] = {"server_file_exists": config.is_file(), "default_source_file_exists": (ROOT / "config/production-gpu.json").is_file()}
    if config.is_file():
        value = json.loads(config.read_text())
        report["gpu_registration"].update({"enabled": value.get("enabled"), "release_id": value.get("transport", {}).get("release_id")})
        # Health/metadata GET only; never send a prediction POST during this audit.
        transport = value.get("transport", {})
        base = transport.get("base_url")
        if base:
            report["gpu_service_readonly"] = {p: get(base.rstrip("/") + p) for p in ["/healthz", "/metadata"]}
    OUT.mkdir(parents=True, exist_ok=True)
    destination = OUT / "runtime-audit.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    destination.chmod(0o600)
    print(json.dumps({"audit": destination.relative_to(ROOT).as_posix(), "release": report["release"], "catalog_http": {k: v.get("http") for k, v in report["catalogs"].items()}}))


if __name__ == "__main__":
    main()
