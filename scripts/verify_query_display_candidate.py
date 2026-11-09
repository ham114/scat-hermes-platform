"""Validate the exact candidate images before switching; no model calls."""

import json
import os
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261107-query-display-completion"


def main():
    plan = json.loads((OUT / "deployment-plan.json").read_text())

    def command(role, network="none"):
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
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "TMPDIR=" + str(ROOT / ".tmp"),
            "--env",
            "TEMP=" + str(ROOT / ".tmp"),
            "--env",
            "TMP=" + str(ROOT / ".tmp"),
        ]

    files = [
        "tests/hermes_runtime/test_trajectory_completion.py",
        "tests/hermes_runtime/test_skill_guard.py",
        "tests/hermes_runtime/test_skills.py",
        "tests/hermes_runtime/test_readonly_display.py",
        "tests/workflows/test_api_and_catalog.py",
        "tests/hermes_runtime/test_standard_profiles.py",
        "tests/hermes_runtime/test_platform_api.py",
    ]
    args = command("business") + [
        "--tmpfs",
        str(ROOT / "var/workflow-inputs") + ":rw,uid=1000,gid=1000,mode=0700",
        "--mount",
        "type=bind,src="
        + str(ROOT / "docs/validation/20261023-skills-reuse-inventory.json")
        + ",dst="
        + str(ROOT / "docs/validation/20261023-skills-reuse-inventory.json")
        + ",readonly",
        "--mount",
        "type=bind,src=" + str(ROOT / "tests") + ",dst=" + str(ROOT / "tests") + ",readonly",
        plan["images"]["business"]["image_id"],
        "-m",
        "pytest",
        "-q",
        "-o",
        "cache_dir=" + str(ROOT / ".tmp/pytest-cache"),
        "--basetemp=" + str(ROOT / ".tmp/pytest"),
        *files,
    ]
    tests = subprocess.run(args, capture_output=True, text=True, timeout=120)
    (OUT / "candidate-image-tests.log").write_text(tests.stdout + tests.stderr)
    assert tests.returncode == 0, "candidate_image_tests_failed"
    probe = (ROOT / "scripts/probe_query_display_deployed.py").read_text()
    marker = "    print(\n"
    assert marker in probe
    probe = probe.replace(
        marker,
        '    catalog = invoke("skills_list", {})\n    assert all("allowed_operations" in item for item in catalog["skills"])\n'
        + marker,
        1,
    )
    args = command("native") + [plan["images"]["native"]["image_id"]]
    native = subprocess.run(args, input=probe, capture_output=True, text=True, timeout=60)
    (OUT / "candidate-native-dispatch.log").write_text(native.stdout + native.stderr)
    assert native.returncode == 0, "candidate_native_dispatch_failed"
    native_report = json.loads(native.stdout.strip().splitlines()[-1])
    assert (
        native_report["denial_contains_recovery"]
        and native_report["new_run_denial_contains_recovery"]
    )
    current = json.loads(
        subprocess.check_output(["docker", "inspect", "scat-hermes-h7b-platform-1"])
    )[0]
    env = dict(item.split("=", 1) for item in current["Config"]["Env"] if "=" in item)
    secret = env["AUTH_SECRET"]
    old = json.loads(
        (ROOT / "var/validation/20261107-query-display-rounds/natural-rounds.json").read_text()
    )["records"]
    case = next(r for r in old if r["case"] == "followup_plot")
    base = f"http://127.0.0.1:18120/api/v1/hermes/threads/{case['thread_id']}/runs/{case['run_id']}/results"

    def get(url):
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Authorization": "Bearer " + secret}), timeout=30
        ) as r:
            return json.load(r)

    ref = next(r for r in get(base)["items"] if r["operation"] == "get_scat_track_points")
    seed = get(base + "/" + ref["result_id"])["result"]
    code = """import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from application.context import ExecutionContext
from application.readonly_extensions import build_extended_business_application
from application.trajectory_view import trajectory_view
from application.readonly_display import receipt_display
from core.settings import settings
from hermes_runtime.results import ToolResultStore
from hermes_runtime.trajectory_reply import coverage
async def main():
 root=Path.cwd();temp=root/'.tmp/candidate-real';temp.mkdir()
 store=ToolResultStore(temp/'results.sqlite',workspace_root=root)
 context=ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID,'candidate-view')
 seed=json.load(sys.stdin)
 ref=store.put(context.scope_key,'run_candidate','seed','get_scat_track_points',seed)
 app=build_extended_business_application()
 try:
  result=await trajectory_view(store,context,'run_candidate',ref['result_id'],app)
  assert result['coverage']['complete'] and result['coverage']['loaded']==524
  async def forbidden(*args,**kwargs):raise AssertionError('refresh_must_not_query')
  cached=await trajectory_view(store,context,'run_candidate',ref['result_id'],SimpleNamespace(invoke=forbidden))
  assert cached==result
  valid=True
  for r in result['items']:
   value=store.get(context.scope_key,'run_candidate',r['result_id']);display=receipt_display(value)
   block=next(b for b in display['analysis_result']['blocks'] if b['type']=='trajectory_2d')
   valid=valid and all(p['position']['lon_deg']==q['longitude'] and p['position']['lat_deg']==q['latitude'] for p,q in zip(value['result']['data']['points'],block['points']))
  assert valid
  assert coverage(store,context.scope_key,'run_candidate')[0]['loaded']==100
  assert store.get(context.scope_key,'run_candidate',ref['result_id'])['result']==seed
  print(json.dumps({'status':'passed','real_database':True,'points':524,'pages':len(result['items']),'coordinate_match':valid,'refresh_database_calls':0,'analysis_scope_points':100,'llm_calls':0,'raw_data_saved':False}))
 finally:await app.aclose()
asyncio.run(main())
"""
    args = command("business", "host")
    # Values travel only through the child environment, never argv or a file.
    for name in env:
        if name not in {"HOSTNAME", "TMPDIR", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE"}:
            args += ["--env", name]
    args += [plan["images"]["business"]["image_id"], "-c", code]
    business = subprocess.run(
        args,
        input=json.dumps(seed),
        capture_output=True,
        text=True,
        env=os.environ | env,
        timeout=90,
    )
    (OUT / "candidate-real-view.log").write_text(business.stdout + business.stderr)
    assert business.returncode == 0, "candidate_real_view_failed"
    report = {
        "status": "passed",
        "image_tests": "131 passed" in tests.stdout,
        "native_recovery_hints": True,
        "native_catalog_operations": True,
        "candidate_real_view": json.loads(business.stdout.strip().splitlines()[-1]),
        "images": {k: v["image_id"] for k, v in plan["images"].items()},
        "production_changed": False,
    }
    assert report["image_tests"]
    (OUT / "candidate-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "images"}))


if __name__ == "__main__":
    main()
