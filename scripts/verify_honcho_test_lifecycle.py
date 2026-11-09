"""Read stored memory across candidate restarts; no paid model replay."""

import hashlib
import json
import math
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "var/deploy/honcho"
OUT = ROOT / "var/validation/20261027-honcho"


def run(*args):
    return subprocess.run(
        list(args), cwd=ROOT, capture_output=True, text=True, check=True, timeout=90
    ).stdout


def usage():
    with sqlite3.connect(DEPLOY / "testing/proxy-budget.sqlite") as db:
        return db.execute(
            "SELECT count(*),COALESCE(sum(reserved),0),COALESCE(sum(input_tokens),0),COALESCE(sum(output_tokens),0) FROM requests"
        ).fetchone()


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    import httpx
    from openai import OpenAI

    checks = {}
    image = json.loads((DEPLOY / "image.lock.json").read_text())["image_id"]
    auth = json.loads((OUT / "memory/auth-recheck.json").read_text())
    if auth["status"] != "passed":
        raise ValueError("auth_checks_required")
    previous = json.loads((OUT / "memory/result.json").read_text())
    if not all(value for name, value in previous["checks"].items() if name not in auth["checks"]):
        raise ValueError("memory_checks_required")
    before = usage()
    key = (DEPLOY / "secrets/embedding.key").read_text().strip()
    with OpenAI(
        base_url="http://127.0.0.1:18141/v1", api_key=key, timeout=60, max_retries=0
    ) as sdk:
        response = sdk.embeddings.create(
            model="bge-m3", input=["合成偏好：简体中文，距离单位公里。"]
        )
    vector = response.data[0].embedding
    checks["SDK_default_base64_decoded_1024"] = (
        len(vector) == 1024
        and all(math.isfinite(x) for x in vector)
        and abs(sum(x * x for x in vector) - 1) < 0.001
    )
    statement = (
        "from pathlib import Path;import json;from honcho import Honcho;k=Path('"
        + str(DEPLOY / "secrets/workspace.key")
        + "').read_text().strip();c=Honcho(base_url='http://127.0.0.1:18140',api_key=k,workspace_id='scat-honcho-test-20261027',max_retries=0,timeout=30);print(json.dumps({'representation':c.peer('scat-researcher').representation()},ensure_ascii=False))"
    )
    mount = (
        "type=bind,source="
        + str(DEPLOY / "secrets/workspace.key")
        + ",target="
        + str(DEPLOY / "secrets/workspace.key")
        + ",readonly"
    )

    def read_memory():
        result = json.loads(
            run(
                "docker",
                "run",
                "--rm",
                "--label",
                "scat.owner=scat-honcho-test",
                "--network",
                "host",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--mount",
                mount,
                image,
                "-c",
                statement,
            )
        )
        return result["representation"]

    original = read_memory()
    # Finish the finite test campaign before restarting the database.
    run("docker", "stop", "--time", "25", "scat-honcho-test-deriver")
    run("docker", "stop", "--time", "25", "scat-honcho-test-api")
    run("docker", "restart", "--time", "25", "scat-honcho-test-db")
    for _ in range(60):
        result = subprocess.run(
            [
                "docker",
                "exec",
                "scat-honcho-test-db",
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "honcho",
                "-d",
                "honcho",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("candidate_database_restart_failed")
    run("docker", "start", "scat-honcho-test-api")
    with httpx.Client(trust_env=False, timeout=2) as client:
        for _ in range(90):
            try:
                if client.get("http://127.0.0.1:18140/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("candidate_api_restart_failed")
    restored = read_memory()
    checks["stored_memory_survives_database_and_api_restart"] = sorted(
        original.splitlines()
    ) == sorted(restored.splitlines()) and ("kilomet" in restored.lower() or "公里" in restored)
    checks["no_paid_model_replay"] = before == usage()
    sql = "SELECT c.relname||':'||a.atttypmod FROM pg_attribute a JOIN pg_class c ON a.attrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='public' AND c.relname IN ('documents','message_embeddings') AND a.attname='embedding' ORDER BY c.relname;"
    dimensions = run(
        "docker", "exec", "scat-honcho-test-db", "psql", "-U", "honcho", "-d", "honcho", "-tAc", sql
    ).splitlines()
    checks["both_pgvector_columns_1024"] = dimensions == [
        "documents:1024",
        "message_embeddings:1024",
    ]
    result = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "before_usage": before,
        "after_usage": usage(),
        "representation_sha256": hashlib.sha256(
            "\n".join(sorted(restored.splitlines())).encode()
        ).hexdigest(),
        "deriver_left_stopped": True,
    }
    (OUT / "lifecycle.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    assert all(checks.values()), "candidate_restart_check_failed"


if __name__ == "__main__":
    main()
