"""Choose a real off-grid cutoff from two bounded observed timestamps, without LLMs."""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
CODE = """import asyncio,json,sys
sys.path.insert(0,'src')
from application.context import ExecutionContext
from application.readonly_extensions import build_extended_business_application
async def main():
 a=build_extended_business_application()
 try: print(json.dumps(await a.invoke('get_scat_track_points',{'batch':'scat20161015_20161021','external_id':'100002','start':220,'limit':2,'every_nth':1},context=ExecutionContext('scat-researcher','readiness-cutoff')),default=str))
 finally: await a.aclose()
asyncio.run(main())
"""


def main():
    os.umask(0o077)
    target = OUT / "asof-plan.json"
    assert not target.exists()
    result = subprocess.run(
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
        text=True,
        capture_output=True,
        check=True,
        timeout=45,
    )
    raw = json.loads(result.stdout)
    attempt = len(list((OUT / "private").glob("asof-source*.json"))) + 1
    (OUT / f"private/asof-source-{attempt}.json").write_text(json.dumps(raw, ensure_ascii=False))
    assert raw["status"] == "ok"
    data = raw["data"]
    points = data.get("points", data.get("items", []))
    assert len(points) == 2, list(data)
    stamps = [datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")) for p in points]
    assert stamps[0] < stamps[1]
    cutoff = stamps[0] + (stamps[1] - stamps[0]) / 2
    plan = {
        "batch": "scat20161015_20161021",
        "external_id": "100002",
        "as_of": cutoff.isoformat(),
        "neighboring_observation_times": [s.isoformat() for s in stamps],
        "observation_last_n": 200,
        "horizons_s": [10, 30, 60],
        "source_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "new_model_calls": 0,
    }
    target.write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan))


if __name__ == "__main__":
    main()
