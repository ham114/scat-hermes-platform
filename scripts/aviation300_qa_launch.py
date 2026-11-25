"""Single no-replay launcher for the authorized bounded aviation QA phases."""
import json,os,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261124-rag-aviation300'
PYTHON='/home/h3c/anaconda3/envs/scat-hermes/bin/python'
def save(v):
    p=OUT/'launch-status.json';tmp=p.with_suffix('.writing')
    tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,p)
def main():
    os.umask(0o077)
    inspection=json.loads((OUT/'qa-inspection.json').read_text())
    assert inspection['result']['status']=='inspection_passed' and inspection['usage']['llm_calls']==0
    policy=json.loads((OUT/'qa-policy.json').read_text())
    assert time.time()<policy['expires_at']
    with (OUT/'launch.admitted.json').open('x') as f:json.dump({'started_at':time.time(),'pid':os.getpid(),'scope':'300 cases calibration then actual QA and fresh-context scoring; shared frozen ledger, no replay'},f)
    for phase,script,args in [('calibration','aviation300_qa_campaign.py',['calibrate']),
        ('qa','aviation300_qa_campaign.py',['run']),('offline-audit','aviation300_qa_audit.py',[])]:
        save({'status':'running','phase':phase,'pid':os.getpid(),'at':time.time()})
        with (OUT/(phase+'.log')).open('a') as log:
            completed=subprocess.run([PYTHON,'-B',str(ROOT/'scripts'/script),*args],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        if completed.returncode:
            save({'status':'stopped_phase_error','phase':phase,'exit_code':completed.returncode,'at':time.time()});return
    save({'status':'completed','phase':'offline-audit','at':time.time()})
if __name__=='__main__':main()
