"""Authenticated read/preview of live management APIs without applying settings or memory reads."""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"


def main():
    row = json.loads(
        subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-author-api-1"])
    )[0]
    env = dict(x.split("=", 1) for x in row["Config"]["Env"] if "=" in x)
    key = (
        (ROOT / env.get("SCAT_AUTHOR_KEY_FILE", "var/secrets/skill-author.key")).read_text().strip()
    )
    headers = {"X-SCAT-Author-Key": key}
    with httpx.Client(trust_env=False, timeout=20) as http:
        base = "http://127.0.0.1:18124"
        assert http.get(base + "/v1/settings").status_code == 401
        assert http.get(base + "/v1/skills/catalog").status_code == 401
        r = http.get(base + "/v1/settings", headers=headers)
        assert r.status_code == 200
        settings = r.json()
        assert len(settings["sections"]) == 8 and settings["runtime_observed"] is False
        assert len(settings["profiles"]) == 3
        r = http.get(base + "/v1/skills/catalog", headers=headers)
        assert r.status_code == 200
        catalog = r.json()
        assert catalog["items"]
        section = next(s for s in settings["sections"] if s["section"] == "memory")
        body = {
            "section": "memory",
            "changes": {"context_chars": section["value"]["context_chars"]},
            "expected_sha256": section["config_sha256"],
            "request_id": str(uuid4()),
        }
        r = http.post(base + "/v1/settings/preview", headers=headers, json=body)
        assert r.status_code == 200
        assert r.json()["application"] == "pending_maintenance"
        rejected = http.post(
            base + "/v1/settings/proposals", headers=headers, json={**body, "confirmed": True}
        )
        assert rejected.status_code == 403
        # A matched protected GET gives 401; no current user's corrections or memory body is read.
        assert (
            http.get("http://127.0.0.1:18120/api/v1/hermes/memory/corrections").status_code == 401
        )
        assert http.get("http://127.0.0.1:18142/health").status_code == 200
    report = {
        "status": "passed",
        "settings_sections": 8,
        "profiles": 3,
        "catalog_skills": len(catalog["items"]),
        "bundle_sha256": catalog["bundle_sha256"],
        "unauthenticated_management": 401,
        "proposal_without_release_key": 403,
        "preview_without_apply": True,
        "configuration_applied": False,
        "real_user_memory_read": False,
        "model_calls_started": 0,
    }
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    for role in ["platform", "author-api"]:
        code = (
            "import hashlib,json;from pathlib import Path;r=Path.cwd();print(json.dumps({p:hashlib.sha256((r/p).read_bytes()).hexdigest() for p in "
            + repr(list(plan["source_hashes"]))
            + "}))"
        )
        values = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "scat-hermes-h7b-" + role + "-1",
                    "/opt/conda/envs/scat-agent/bin/python",
                    "-B",
                    "-c",
                    code,
                ]
            )
        )
        assert values == plan["source_hashes"]
    report["runtime_source_hashes_per_role"] = len(plan["source_hashes"])
    (OUT / "management-live.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
