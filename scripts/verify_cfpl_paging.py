"""Re-read two CFPL pages after indexing; compare exact frozen payloads, without an LLM."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
CODE = r"""
import asyncio,json,time,sys
from application.readonly_extensions import build_extended_business_application
from application.context import ExecutionContext
from core.settings import settings
rows=json.load(sys.stdin)
async def main():
    app=build_extended_business_application();result=[]
    try:
        for row in rows:
            start=time.monotonic();value=await app.invoke(row['operation'],row['arguments'],context=ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID,'readiness-cfpl-index-read'))
            result.append({'operation':row['operation'],'offset':row['arguments']['offset'],'status':value.get('status'),'seconds':round(time.monotonic()-start,4),'same_frozen_payload':value==row['result'],'before_seconds':row['seconds']})
    finally:await app.aclose()
    print(json.dumps(result))
asyncio.run(main())
"""


def main():
    os.umask(0o077)
    assert not (OUT / "cfpl-paging-performance.json").exists()
    frozen = json.loads((OUT / "private/bounded-business.json").read_text())
    selected = [r for r in frozen if r["operation"] == "get_worst_prediction_cases"]
    p = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "scat-hermes-h7b-platform-1",
            "/opt/conda/envs/scat-agent/bin/python",
            "-B",
            "-c",
            CODE,
        ],
        input=json.dumps(selected),
        capture_output=True,
        text=True,
        check=False,
        timeout=80,
    )
    (OUT / "private/cfpl-paging-performance.stderr").write_text(p.stderr)
    assert p.returncode == 0, "cfpl_read_failed"
    report = {
        "kind": "real_deterministic_read_no_external_model",
        "cases": json.loads(p.stdout),
        "new_model_calls": 0,
    }
    assert all(r["same_frozen_payload"] and r["status"] == "ok" for r in report["cases"])
    (OUT / "cfpl-paging-performance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
