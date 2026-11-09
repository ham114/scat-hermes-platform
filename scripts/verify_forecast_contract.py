"""Check the exact candidate images and real chronological tail without model calls."""

import json
import os
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-forecast-contract"


def command(plan, role, *, network="none"):
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        network,
        "--read-only",
        "--entrypoint",
        "/opt/conda/envs/scat-agent/bin/python"
        if role == "business"
        else "/opt/conda/envs/scat-hermes/bin/python",
        "--tmpfs",
        str(ROOT / ".tmp") + ":rw,uid=1000,gid=1000,mode=0700",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "TMPDIR=" + str(ROOT / ".tmp"),
    ]


def main():
    plan = json.loads((OUT / "deployment-plan.json").read_text())
    code = """import ast,hashlib,json
from pathlib import Path
from workflow_runtime.catalog import schemas,OPERATIONS
from workflow_runtime.contracts import WorkflowProposal
r=Path.cwd(); saved=json.loads((r/'services/hermes/workflow-operations.v1.json').read_text())
assert saved['controls']==schemas(controls=True)
assert saved['operations']==[{**v,'toolset':OPERATIONS[v['name']][0],'effect':OPERATIONS[v['name']][4]} for v in schemas()]
assert 'forecast' in WorkflowProposal.model_json_schema()['properties']['kind']['enum']
tree=ast.parse((r/'src/application/skill_jobs.py').read_text()); n=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SOURCES' for t in n.targets))
paths=ast.literal_eval(n.value); hashes={p:hashlib.sha256((r/p).read_bytes()).hexdigest() for p in paths}
assert 'src/workflow_runtime/windows.py' in hashes and 'src/workflow_runtime/forecast.py' in hashes
print(json.dumps(hashes))
"""
    fingerprints = []
    for role in ["business", "native"]:
        result = subprocess.run(
            command(plan, role) + [plan["images"][role]["image_id"]],
            input=code,
            capture_output=True,
            text=True,
            timeout=60,
        )
        (OUT / ("candidate-schema-" + role + ".log")).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, role + "_schema_probe_failed"
        fingerprints.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert fingerprints[0] == fingerprints[1], "role_implementation_fingerprints_differ"
    native_args = command(plan, "native") + [
        "--mount",
        "type=bind,src="
        + str(ROOT / "var/deploy/h7b/shared/skills")
        + ",dst="
        + str(ROOT / "var/deploy/h7b/shared/skills")
        + ",readonly",
        "-e",
        "SCAT_SKILL_RELEASE_STATE=var/deploy/h7b/shared/skills/catalog.sqlite",
        "-e",
        "FORECAST_TARGET_BUNDLE=" + plan["skill_change"]["target_sha"],
        "-e",
        "FORECAST_PREVIOUS_BUNDLE="
        + json.loads((OUT / "first-deployment-plan.json").read_text())["skill_change"][
            "previous_sha"
        ],
        plan["images"]["native"]["image_id"],
    ]
    native = subprocess.run(
        native_args,
        input=(ROOT / "scripts/probe_forecast_native_restart.py").read_text(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    (OUT / "candidate-native-restart.log").write_text(native.stdout + native.stderr)
    assert native.returncode == 0, "native_restart_over_old_home_failed"
    restart = json.loads(native.stdout.strip().splitlines()[-1])
    assert restart["old_home_retained"] and restart["versioned_folder"]
    args = command(plan, "business") + [
        "--tmpfs",
        str(ROOT / "var/validation") + ":rw,uid=1000,gid=1000,mode=0700",
        "--tmpfs",
        str(ROOT / "var/workflow-inputs") + ":rw,uid=1000,gid=1000,mode=0700",
        "--mount",
        "type=bind,src=" + str(ROOT / "tests") + ",dst=" + str(ROOT / "tests") + ",readonly",
        "--mount",
        "type=bind,src="
        + str(ROOT / "docs/validation/20261023-skills-reuse-inventory.json")
        + ",dst="
        + str(ROOT / "docs/validation/20261023-skills-reuse-inventory.json")
        + ",readonly",
        plan["images"]["business"]["image_id"],
        "-m",
        "pytest",
        "-q",
        "-o",
        "cache_dir=" + str(ROOT / ".tmp/pytest-cache"),
        "--basetemp=" + str(ROOT / ".tmp/pytest"),
        "tests/workflows/test_forecast_contract.py",
        "tests/workflows/test_forecast_gpu_contract.py",
        "tests/workflows/test_contracts_and_numerics.py",
        "tests/hermes_runtime/test_skill_guard.py",
        "tests/analysis_runs",
        "tests/workflows/test_approved_workflows.py::test_forecast_checks_legacy_artifact_index_before_sampling_or_model_effects",
        "tests/workflows/test_api_and_catalog.py",
        "tests/workflows/test_approved_workflows.py::test_complete_workflows_publish_real_standard_results[forecast]",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=150)
    (OUT / "candidate-tests.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, "exact_candidate_tests_failed"
    live = json.loads(subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"]))[
        0
    ]
    environment = dict(s.split("=", 1) for s in live["Config"]["Env"] if "=" in s)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:18120/api/v1/hermes/threads/56f86948-596f-476e-9925-3e67ae0e5427/runs/run_8c791f26c2454c2baa3d759f81045eeb/results"

    def get(url):
        with opener.open(
            urllib.request.Request(
                url, headers={"Authorization": "Bearer " + environment["AUTH_SECRET"]}
            ),
            timeout=30,
        ) as response:
            return json.load(response)

    references = get(base)["items"]
    query = get(base + "/" + references[0]["result_id"])["result"]["query"]
    real_code = """import asyncio,json
from application.context import ExecutionContext
from workflow_runtime.contracts import WorkflowProposal
from workflow_runtime.data import ScatSource,select_input,json_value
async def main():
    source=ScatSource()
    p=WorkflowProposal(kind='forecast',batch=BATCH,external_ids=(FLIGHT,),models=({'model':'constant-velocity-heading'},),observation_last_n=200,horizons_s=(10,30,60))
    frozen=await select_input(ExecutionContext('candidate-read-only','exact-tail'),p,source)
    flight=frozen['flights'][0]; raw=await source.repository.get_gbar_input_candidates(batch=BATCH,external_id=FLIGHT,limit=201)
    assert flight['data']['feature_points']==json_value(raw['points'][-200:])
    window=flight['observation_windows'][0]
    assert window['point_count']==200 and len(flight['data']['plots'])==200
    print(json.dumps({'real_database':True,'exact_points':200,'independent_201_point_tail_match':True,'origin_equals_final_tail_point':window['forecast_origin']==flight['data']['plots'][-1]['timestamp'].replace('Z','+00:00') if 'T' in flight['data']['plots'][-1]['timestamp'] else True,'span_s':window['span_s'],'llm_calls':0,'prediction_calls':0,'raw_data_saved':False}))
asyncio.run(main())
"""
    # The comparison below uses datetime normalization, not timestamp string spelling.
    real_code = real_code.replace(
        "window['forecast_origin']==flight['data']['plots'][-1]['timestamp'].replace('Z','+00:00') if 'T' in flight['data']['plots'][-1]['timestamp'] else True",
        "__import__('scat.prediction.alignment',fromlist=['build_track']).build_track(flight['data']['plots'])[0][-1].time.isoformat()==window['forecast_origin']",
    )
    real_code = (
        "BATCH="
        + repr(query["batch"])
        + "\nFLIGHT="
        + repr(str(query["external_id"]))
        + "\n"
        + real_code
    )
    args = command(plan, "business", network="host")
    for name in environment:
        args += ["-e", name]
    result = subprocess.run(
        args + [plan["images"]["business"]["image_id"]],
        input=real_code,
        capture_output=True,
        text=True,
        env=os.environ | environment,
        timeout=60,
        check=True,
    )
    real = json.loads(result.stdout.strip().splitlines()[-1])
    assert real["origin_equals_final_tail_point"]
    report = {
        "status": "passed",
        "release": plan["release"],
        "role_fingerprint_match": True,
        "role_fingerprint_files": len(fingerprints[0]),
        "schema_match_both_roles": True,
        "native_restart": restart,
        "candidate_real_tail": real,
        "production_changed": False,
    }
    (OUT / "candidate-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
