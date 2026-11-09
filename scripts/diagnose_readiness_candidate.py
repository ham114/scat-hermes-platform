"""Import-only candidate diagnosis; no listener, lifespan worker, model or paid request."""
import json
import subprocess
from pathlib import Path

import deploy_release_readiness as deploy

ROOT,OUT=deploy.ROOT,deploy.OUT

def main():
    plan=json.loads((OUT/'deployment-plan.json').read_text())
    folder=deploy.DEPLOY/'releases'/plan['release']
    report=[]
    for service,python,code in [
        ('platform','/opt/conda/envs/scat-agent/bin/python','import service.hermes_app;print("candidate_platform_imported")'),
        ('hermes-main','/opt/conda/envs/scat-hermes/bin/python','import runpy;v=runpy.run_path("scripts/run_hermes_api.py",run_name="diagnostic");print(len(v["business_tool_names"](True)))'),
    ]:
        args=deploy.compose(folder/'compose.yaml')+['run','--rm','--no-deps','--entrypoint',python,service,'-B','-c',code]
        result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,check=False,timeout=45)
        (OUT/('candidate-import-'+service+'.log')).write_text(result.stdout+result.stderr)
        report.append({'service':service,'code':result.returncode,'stdout':result.stdout[-300:],'stderr':result.stderr[-1300:]})
    print(json.dumps(report))

if __name__=='__main__':main()
