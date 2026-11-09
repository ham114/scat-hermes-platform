"""Bounded read-only checks on the live image; export only status/count/hash metadata."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-platform-closeout"
CODE = r"""
import asyncio,hashlib,json,time,sys
sys.path.insert(0,"src")
from application.context import ExecutionContext
from application.readonly_extensions import build_extended_business_application
from core.settings import settings

async def main():
    app=build_extended_business_application()
    context=ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID,"closeout-backend-readonly")
    rows=[]
    async def call(name,args):
        started=time.monotonic();result=await app.invoke(name,args,context=context)
        data=result.get("data") or {}
        rows.append({"operation":name,"status":result.get("status"),"seconds":round(time.monotonic()-started,3),
          "list_lengths":{k:len(v) for k,v in data.items() if isinstance(v,list)} if isinstance(data,dict) else {},
          "payload_sha256":hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()})
        return result
    try:
        ref={"batch":"scat20161015_20161021","external_id":"100002"}
        await call("list_scat_batches",{"limit":1})
        await call("get_scat_batch_overview",{"batch":ref["batch"]})
        await call("get_scat_flight_overview",ref)
        await call("get_scat_track_points",{**ref,"limit":3})
        await call("inspect_scat_model_data_quality",{**ref,"source_limit":200})
        await call("get_scat_fpl_overview",ref)
        await call("get_scat_prediction_overview",ref)
        snapshots=await call("list_scat_prediction_snapshots",{**ref,"limit":2})
        items=(snapshots.get("data") or {}).get("snapshots",[])
        if items:await call("get_scat_prediction_snapshot",{**ref,"timestamp":items[0]["time"]})
        await call("get_scat_flight_phase_stats",ref)
        await call("get_scat_prediction_error_stats",ref)
        await call("list_uav_models",{})
        await call("list_uav_channels",{})
        found=await call("search_uav_flights",{"limit":1})
        flights=(found.get("data") or {}).get("items",[])
        if flights:
            uav={"external_id":flights[0]["external_id"]}
            await call("get_uav_flight_overview",uav)
            await call("get_uav_samples",{**uav,"limit":3,"fields":["position"]})
            await call("get_uav_phase_stats",uav)
        report={"status":"passed" if all(r["status"]=="ok" for r in rows) else "gaps_found",
          "evidence":"direct_backend_operations_on_live_image_not_natural_language",
          "registered_readonly_operations":len(app.specs),"checked":len(rows),"operations":rows,
          "new_gbar_predictions":0,"new_llm_calls":0,"raw_business_data_exported":False}
        print(json.dumps(report))
    finally:await app.aclose()
asyncio.run(main())
"""


def main():
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "scat-hermes-h7b-platform-1",
            "/opt/conda/envs/scat-agent/bin/python",
            "-B",
            "-",
        ],
        input=CODE,
        capture_output=True,
        text=True,
        check=False,
        timeout=150,
    )
    (OUT / "business-readonly.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, "business_probe_failed_see_metadata_log"
    report = json.loads(result.stdout.strip().splitlines()[-1])
    (OUT / "business-readonly.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checked": report["checked"],
                "failures": [r for r in report["operations"] if r["status"] != "ok"],
            }
        )
    )


if __name__ == "__main__":
    main()
