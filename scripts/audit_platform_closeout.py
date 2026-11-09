"""Read-only deployment and source inventory; never print environment secrets or memories."""

import ast
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"
FILES = [
    "src/service/memory_api.py",
    "src/hermes_runtime/honcho_memory.py",
    "src/hermes_runtime/experience_learning.py",
    "src/hermes_runtime/agent_settings.py",
    "src/service/agent_settings_api.py",
    "src/service/skill_authoring_api.py",
    "src/hermes_runtime/profiles.py",
    "src/hermes_runtime/native_profiles.py",
    "src/service/hermes_app.py",
    "src/hermes_runtime/autonomy_config.py",
    "scripts/run_skill_authoring_api.py",
    "scripts/run_hermes_api.py",
    "src/hermes_runtime/daily_budget.py",
    "scripts/run_autonomy_proxy.py",
]


def main():
    report = {"status": "inspected", "model_calls_started": 0, "roles": {}}
    for name in ["platform", "author-api", "hermes-main", "hermes-author"]:
        row = json.loads(
            subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-" + name + "-1"])
        )[0]
        env = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x)
        files = {}
        for path in FILES:
            result = subprocess.run(
                ["docker", "exec", row["Id"], "cat", str(ROOT / path)],
                capture_output=True,
                check=False,
            )
            local = ROOT / path
            sha = hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None
            files[path] = {
                "runtime_sha256": sha,
                "matches_source": local.is_file()
                and sha == hashlib.sha256(local.read_bytes()).hexdigest(),
            }
            if path in {"src/service/memory_api.py", "src/service/skill_authoring_api.py"} and sha:
                files[path]["routes"] = sorted(
                    {
                        d.args[0].value
                        for node in ast.walk(ast.parse(result.stdout))
                        for d in getattr(node, "decorator_list", [])
                        if isinstance(d, ast.Call)
                        and d.args
                        and isinstance(d.args[0], ast.Constant)
                        and isinstance(d.args[0].value, str)
                    }
                )
        report["roles"][name] = {
            "image": row["Image"],
            "health": row["State"].get("Health", {}).get("Status"),
            "files": files,
            "mounts": [{k: m[k] for k in ["Source", "Destination", "RW"]} for m in row["Mounts"]],
            "flags": {
                k: env[k]
                for k in [
                    "SCAT_AUTONOMY_CONFIG",
                    "SCAT_AGENT_SETTINGS_MANIFEST",
                    "SCAT_PROFILE_ROOT",
                ]
                if k in env
            },
        }
    manifest = json.loads((ROOT / "config/agent-settings-targets.example.json").read_text())
    report["settings_target_presence"] = {
        k: (ROOT / v / "profiles" / k / "config.yaml").is_file()
        for k, v in manifest["profiles"].items()
    }
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        report["budget_by_state"] = db.execute(
            "SELECT state,count(*),sum(reserved) FROM model_calls GROUP BY state"
        ).fetchall()
    (OUT / "runtime-inventory.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "differences": {
                    r: [p for p, v in d["files"].items() if not v["matches_source"]]
                    for r, d in report["roles"].items()
                },
                "settings_target_presence": report["settings_target_presence"],
                "budget_by_state": report["budget_by_state"],
            }
        )
    )


if __name__ == "__main__":
    main()
