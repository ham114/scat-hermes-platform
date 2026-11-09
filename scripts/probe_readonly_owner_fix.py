"""Live shared-data owner admission plus unknown owner denial; no model calls."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness/owner-fix"
CODE = '''import asyncio,json,sys
sys.path.insert(0,'src')
from application.context import ExecutionContext
from application.readonly_extensions import build_extended_business_application
async def main():
 a=build_extended_business_application();rows=[]
 try:
  for owner in ['scat-researcher','scat-zyj','scat-admin','unregistered']:
   r=await a.invoke('get_scat_track_points',{'batch':'scat20161015_20161021','external_id':'100002','start':220,'limit':2},context=ExecutionContext(owner,'readiness-owner-probe'))
   assert r['status']==('forbidden' if owner=='unregistered' else 'ok')
   rows.append({'owner':owner,'status':r['status'],'points':len((r.get('data') or {}).get('points',[]))})
 finally: await a.aclose()
 print(json.dumps({'status':'passed','cases':rows,'new_model_calls':0,'scope':'live application shared-data admission, signed route identity separately tested'}))
asyncio.run(main())
'''

if __name__ == "__main__":
    assert json.loads((OUT / "deployment-result.json").read_text())["status"] == "applied_behind_gate"
    result = subprocess.run(["docker", "exec", "-i", "scat-hermes-h7b-platform-1", "/opt/conda/envs/scat-agent/bin/python", "-B", "-c", CODE], capture_output=True, text=True, check=True, timeout=45)
    report = json.loads(result.stdout)
    (OUT / "live-owner-admission.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
