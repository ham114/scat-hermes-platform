"""Freeze bounded real quality/CFPL pages using candidate deterministic code, without LLMs."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
CODE = r"""
import asyncio,hashlib,json,sys,time,importlib
sys.path.insert(0,'src')
request=json.load(sys.stdin)
for name,source in request['modules']:
    module=importlib.import_module(name)
    exec(compile(source,name+'.candidate','exec'),module.__dict__)
from application.context import ExecutionContext
from application.readonly_extensions import build_extended_business_application
from application.readonly_display import receipt_display
from core.settings import settings
async def main():
    app=build_extended_business_application();rows=[]
    context=ExecutionContext(settings.ANALYSIS_SINGLE_USER_ID,'readiness-bounded-data')
    batch='scat20161015_20161021'
    cases=[]
    for op in ['list_scat_quality_problem_flights','list_scat_quality_clean_flights','get_prediction_metric_summaries','get_worst_prediction_cases']:
        for offset in [0,2]: cases.append((op,{'batch':batch,'limit':2,'offset':offset}))
    cases += [('search_scat_flights',{'batch':batch,'external_id':'READINESS-NO-SUCH-FLIGHT','limit':2}),('search_scat_flights',{'batch':batch,'external_id':'100002','limit':2})]
    try:
        for i,(op,args) in enumerate(cases):
            start=time.monotonic();result=await app.invoke(op,args,context=context)
            payload=json.dumps(result,sort_keys=True,ensure_ascii=False,default=str)
            sha=hashlib.sha256(payload.encode()).hexdigest()
            receipt={'operation':op,'result':result,'result_id':format(i+1,'032x'),'payload_sha256':sha}
            projected=receipt_display(receipt)
            rows.append({'operation':op,'arguments':args,'result':result,'analysis_result':projected.get('analysis_result'),'payload_sha256':sha,'seconds':round(time.monotonic()-start,4)})
    finally: await app.aclose()
    print(json.dumps(rows,ensure_ascii=False,default=str))
asyncio.run(main())
"""


def main():
    os.umask(0o077)
    private = OUT / "private"
    private.mkdir(exist_ok=True)
    source = private / "bounded-business.json"
    assert not source.exists(), "already_frozen_do_not_requery"
    modules = []
    for name, path in [
        ("scat.services.materialization_service", "src/scat/services/materialization_service.py"),
        ("application.readonly_extensions", "src/application/readonly_extensions.py"),
        ("agents.analysis_results", "src/agents/analysis_results.py"),
        ("application.readonly_display", "src/application/readonly_display.py"),
    ]:
        modules.append([name, (ROOT / path).read_text()])
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
        input=json.dumps({"modules": modules}),
        text=True,
        check=False,
        capture_output=True,
        timeout=400,
    )
    (private / "bounded-business.stderr").write_text(result.stderr)
    assert result.returncode == 0, "bounded_probe_failed_see_private_stderr"
    rows = json.loads(result.stdout)
    source.write_text(json.dumps(rows, ensure_ascii=False))
    public = []
    for row in rows:
        data = row["result"].get("data") or {}
        items = data.get("items", data.get("summaries", data.get("samples", [])))
        public.append(
            {
                "operation": row["operation"],
                "arguments": row["arguments"],
                "status": row["result"].get("status"),
                "returned": len(items),
                "pagination": data.get("pagination"),
                "payload_sha256": row["payload_sha256"],
                "seconds": row["seconds"],
                "blocks": [b["type"] for b in (row["analysis_result"] or {}).get("blocks", [])],
            }
        )
    report = {
        "kind": "real_bounded_readonly_not_natural_language",
        "new_model_calls": 0,
        "cases": public,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    (OUT / "bounded-business.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
