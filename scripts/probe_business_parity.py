"""Freeze bounded real readonly payloads on the server and compare display projections.

Raw payloads remain server-private. Rechecking never queries data or runs inference.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-business-parity"
CODE = r"""
import asyncio,hashlib,json,sys,time
sys.path.insert(0,"src")
request=json.load(sys.stdin)
from application.context import ExecutionContext
from application.readonly_extensions import build_extended_business_application
from core.settings import settings
from agents import analysis_results
legacy={k:getattr(analysis_results,k) for k in dir(analysis_results) if k.startswith("build_")}
exec(compile(request["builders"],"candidate_analysis_results.py","exec"),analysis_results.__dict__)
from application import readonly_display
exec(compile(request["projection"],"candidate_readonly_display.py","exec"),readonly_display.__dict__)
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
async def main():
    rows=request.get("frozen") or []
    if request["action"]=="freeze":
        app=build_extended_business_application()
        context=ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID,"business-parity-readonly")
        async def call(name,args):
            start=time.monotonic()
            value=await app.invoke(name,args,context=context)
            rows.append({"operation":name,"arguments":args,"result":value,"seconds":round(time.monotonic()-start,4)})
            return value
        try:
            batch="scat20161015_20161021"; ref={"batch":batch,"external_id":"100002"}
            for name,args in [
                ("list_scat_batches",{"limit":1}),
                ("get_scat_database_overview",{}),
                ("search_scat_flights",{"batch":batch,"limit":2}),
                ("get_scat_batch_overview",{"batch":batch,"distribution_limit":3}),
                ("get_scat_flight_overview",ref),
                ("get_scat_track_points",{**ref,"start":10,"limit":20,"every_nth":2}),
                ("inspect_scat_model_data_quality",{**ref,"source_limit":200}),
                ("get_scat_airport_stats",{"batch":batch,"limit":3}),
                ("get_scat_fpl_overview",ref),
                ("get_scat_prediction_overview",ref),
                ("get_scat_flight_phase_stats",ref),
                ("get_scat_prediction_error_stats",ref),
                ("list_uav_models",{}),
                ("list_uav_channels",{}),
                ("get_uav_channel_stats",{"channels":["cmp_comb_alt"],"model":"0001"}),
                ("get_uav_normal_vs_anomaly_stats",{"channels":["cmp_comb_alt"]}),
                ("search_scat_knowledge",{"query":"SCAT 数据集时间范围","limit":2}),
            ]: await call(name,args)
            snapshots=await call("list_scat_prediction_snapshots",{**ref,"limit":2})
            items=(snapshots.get("data") or {}).get("snapshots",[])
            if items: await call("get_scat_prediction_snapshot",{**ref,"timestamp":items[0]["time"]})
            found=await call("search_uav_flights",{"limit":2})
            flights=(found.get("data") or {}).get("items",[])
            if flights:
                uav={"external_id":flights[0]["external_id"]}
                await call("get_uav_flight_overview",uav)
                await call("get_uav_samples",{**uav,"start":10,"limit":20,"fields":["position"]})
                await call("get_uav_phase_stats",uav)
            # Empty and invalid cases are different from successful data cases.
            await call("search_uav_flights",{"external_id":"invalid"})
            await call("search_scat_flights",{"batch":batch,"external_id":"PARITY-NO-SUCH-FLIGHT","limit":2})
        finally: await app.aclose()
    report=[]
    for i,row in enumerate(rows):
        raw=row["result"]; sha=digest(raw)
        receipt={"operation":row["operation"],"result":raw,"result_id":format(i+1,"032x"),"payload_sha256":sha}
        before=digest(receipt)
        try:
            projected=readonly_display.receipt_display(receipt)
            analysis=projected.get("analysis_result")
            entry={"operation":row["operation"],"status":raw.get("status"),"payload_sha256":sha,
                "seconds":row["seconds"],"blocks":[b["type"] for b in analysis["blocks"]] if analysis else [],
                "immutable":digest(receipt)==before and projected["result"]==raw}
            if analysis:
                builder=readonly_display.DISPLAY_BUILDERS[row["operation"]]
                old=legacy.get(builder.__name__)
                entry["same_frozen_legacy_blocks"]=(old(raw).model_dump(mode="json")["blocks"]==analysis["blocks"]) if old else None
                entry["analysis_sha256"]=digest(analysis)
                entry["scope"]=analysis["context"]["display_scope"]
                entry["source_matches"]=analysis["context"]["readonly_payload_sha256"]==sha
            row["analysis_result"]=analysis
        except Exception as exc:
            entry={"operation":row["operation"],"status":raw.get("status"),"projection_error":type(exc).__name__}
        report.append(entry)
    print(json.dumps({"frozen":rows,"report":report},ensure_ascii=False,default=str))
asyncio.run(main())
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "verify"])
    args = parser.parse_args()
    os.umask(0o077)
    private = OUT / "private"
    private.mkdir(parents=True, exist_ok=True)
    source = private / "readonly-frozen.json"
    if args.action == "freeze" and source.exists():
        raise ValueError("already_frozen_use_verify")
    request = {
        "action": args.action,
        "builders": (ROOT / "src/agents/analysis_results.py").read_text(),
        "projection": (ROOT / "src/application/readonly_display.py").read_text(),
        "frozen": json.loads(source.read_text()) if args.action == "verify" else None,
    }
    proc = subprocess.run(
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
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=400,
        check=False,
    )
    (private / (args.action + ".stderr")).write_text(proc.stderr)
    if proc.returncode:
        raise RuntimeError("probe_failed_see_server_private_stderr")
    value = json.loads(proc.stdout)
    (private / (args.action + "-display.json")).write_text(
        json.dumps(value["frozen"], ensure_ascii=False)
    )
    if args.action == "freeze":
        source.write_text(json.dumps(value["frozen"], ensure_ascii=False))
    report = {
        "kind": "real_readonly_frozen_projection_not_natural_language",
        "new_inference_calls": 0,
        "raw_payloads_exported": False,
        "cases": value["report"],
        "frozen_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    (OUT / (args.action + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
