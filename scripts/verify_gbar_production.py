"""Zero-inference verification of candidate/live GPU registration and persistence."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-gbar-live"

CODE = """
import asyncio,json,os,sqlite3
from pathlib import Path
from workflow_runtime.gpu import registration,GPURequests
from analysis_runs.artifact_repository import AnalysisArtifactRepository
from scat.infrastructure.database import flight_database
from hermes_runtime.release_store import ReleaseStore
r=Path.cwd(); c=registration(r,required=True); b=c.backend()
try: assert b.health().ready
finally: b.close()
rows=GPURequests(r,c).records(); assert not any(x['state'] in {'running','unknown'} for x in rows)
async def check():
    repo=AnalysisArtifactRepository(); await repo.require_multipage()
    indexes=await repo.collection.index_information(); name=repo.collection.name
    assert any(x.get('unique') and x['key']==[('artifact_id',1)] for x in indexes.values())
    assert any(x.get('expireAfterSeconds')==0 for x in indexes.values())
    await flight_database.close()
    return {'collection':name,'index_names':sorted(indexes)}
print(json.dumps({'status':'passed','gpu':c.pin(),'ledger':str(GPURequests(r,c).path),'receipts':len(rows),'artifacts':asyncio.run(check()),'active_skill':ReleaseStore(r).current().bundle_sha256}))
"""


def main():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    candidate = sys.argv[1] == "candidate"
    if candidate:
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network=host",
            "--read-only",
            "--user",
            "1000:1000",
            "--volumes-from",
            "scat-hermes-h7b-platform-1:rw",
            "--mount",
            "type=bind,source=" + str(ROOT / "var/gpu") + ",target=" + str(ROOT / "var/gpu"),
            "--mount",
            "type=bind,source="
            + str(ROOT / "var/gpu/production.json")
            + ",target="
            + str(ROOT / "var/gpu/production.json")
            + ",readonly",
            "-w",
            str(ROOT),
        ]
        row = json.loads(
            subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"])
        )[0]
        for item in row["Config"]["Env"]:
            if not item.startswith("SCAT_GPU_CONFIG_FILE="):
                command += ["-e", item]
        command += [
            "-e",
            "SCAT_GPU_CONFIG_FILE=var/gpu/production.json",
            "--entrypoint",
            "/opt/conda/envs/scat-agent/bin/python",
            plan["images"]["business"]["image_id"],
            "-B",
            "-",
        ]
    else:
        command = [
            "docker",
            "exec",
            "-i",
            "scat-hermes-h7b-platform-1",
            "/opt/conda/envs/scat-agent/bin/python",
            "-B",
            "-",
        ]
    result = subprocess.run(
        command, input=CODE, capture_output=True, text=True, timeout=90, check=False
    )
    if result.returncode:
        print(result.stderr[-5000:])
        raise RuntimeError("GPU profile verification failed")
    report = json.loads(result.stdout.splitlines()[-1])
    report["source_hashes_verified"] = {}
    for role in ["business", "native"]:
        code = (
            "import hashlib,json;from pathlib import Path;r=Path.cwd(); print(json.dumps({p:hashlib.sha256((r/p).read_bytes()).hexdigest() for p in "
            + repr(list(plan["source_hashes"]))
            + "}))"
        )
        actual = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network=none",
                    "--read-only",
                    "-w",
                    str(ROOT),
                    "--entrypoint",
                    "/opt/conda/envs/"
                    + ("scat-agent" if role == "business" else "scat-hermes")
                    + "/bin/python",
                    plan["images"][role]["image_id"],
                    "-B",
                    "-c",
                    code,
                ],
                text=True,
                timeout=30,
            )
        )
        assert actual == plan["source_hashes"]
        report["source_hashes_verified"][role] = len(actual)
    (OUT / ("candidate-verification.json" if candidate else "live-verification.json")).write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
