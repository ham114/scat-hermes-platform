"""No-model candidate checks for the exact settings mounts and frozen source patch."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"


def main():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    folder = ROOT / "var/deploy/autonomy/releases" / plan["release"]
    compose = json.loads((folder / "compose.yaml").read_text())
    service = compose["services"]["author-api"]
    cmd = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=1000:1000",
        "--cap-drop=ALL",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--workdir=" + str(ROOT),
    ]
    for m in service["volumes"]:
        assert m["type"] == "bind"
        cmd += [
            "--mount=type=bind,src="
            + m["source"]
            + ",dst="
            + m["target"]
            + (",readonly" if m["read_only"] else "")
        ]
    code = """import json,hashlib,ast
from pathlib import Path
from hermes_runtime.agent_settings import AgentSettingsStore
r=Path.cwd()
s=AgentSettingsStore(r,"var/deploy/autonomy/settings-targets.json").snapshot("deployment-validation")
assert len(s["sections"])==8 and s["runtime_observed"] is False
assert all(p["directories_valid"] and p["key_configured"] for p in s["profiles"])
expected=PATCH_HASHES
assert all(hashlib.sha256((r/p).read_bytes()).hexdigest()==sha for p,sha in expected.items())
print(json.dumps({"status":"passed","settings_sections":len(s["sections"]),"profiles":len(s["profiles"]),"source_hashes":len(expected),"model_calls_started":0}))
""".replace("PATCH_HASHES", repr(plan["source_hashes"]))
    cmd += [
        "--entrypoint=/opt/conda/envs/scat-agent/bin/python",
        plan["images"]["author"]["image_id"],
        "-B",
        "-c",
        code,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=40)
    (OUT / "management-candidate.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, "candidate_validation_failed_see_private_free_log"
    report = json.loads(result.stdout)
    (OUT / "management-candidate.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
