"""One-shot continuation after approved native-metric pilot; no fresh budget."""
import json,os,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT.parent/'ragas-evaluation/reports/standard-reeval-20261124'
PYTHON='/home/h3c/anaconda3/envs/ragas-eval/bin/python'
def save(v):
    p=OUT/'launch-status.json';t=p.with_suffix('.writing');t.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n');os.replace(t,p)
def main():
    os.umask(0o077)
    assert json.loads((OUT/'approval.json').read_text())['approved']
    pilot=json.loads((OUT/'pilot-summary.json').read_text())
    assert len(pilot['results'])==40 and all(r['status'] in ['scored','not_applicable_no_delivered_answer'] for r in pilot['results'])
    with (OUT/'launch.admitted.json').open('x') as f:json.dump({'at':time.time(),'pid':os.getpid(),'pilot_requests_reused':pilot['budget']['requests']},f)
    for phase,script,args in [('standard-metrics','ragas_standard_rescore.py',['run']),('offline-audit','ragas_rescore_audit.py',[])]:
        save({'status':'running','phase':phase,'at':time.time(),'pid':os.getpid()})
        with (OUT/(phase+'.log')).open('a') as log:
            result=subprocess.run([PYTHON,'-B',str(ROOT/'scripts'/script),*args],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:
            save({'status':'phase_error','phase':phase,'exit_code':result.returncode,'at':time.time()});return
    save({'status':'completed','phase':'offline-audit','at':time.time()})
if __name__=='__main__':main()
