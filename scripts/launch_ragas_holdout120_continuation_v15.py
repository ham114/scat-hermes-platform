"""Start one detached, sequential continuation within the explicit new approval."""
import fcntl,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-ragas-holdout120'
PY='/home/h3c/anaconda3/envs/ragas-eval/bin/python'

def main():
 assert sys.executable==PY
 if '--worker' in sys.argv:
  with (OUT/'continuation-worker.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   for flags in [['--manual-only'],[]]:
    subprocess.run([PY,'scripts/ragas_holdout120_revision15.py',*flags],cwd=ROOT,check=True)
    from ragas_holdout_budget_extension_v4 import ExtendedBudget
    snap=ExtendedBudget(OUT).snapshot()
    if snap['exhausted'] or snap['deadline_passed']:break
  return
 with (OUT/'continuation-launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  marker=OUT/'continuation-launch-15.json'
  if marker.exists():
   print(json.dumps({'already_submitted':True,**json.loads(marker.read_text())}));return
  from ragas_holdout_budget_extension_v4 import ExtendedBudget
  b=ExtendedBudget(OUT);s=b.snapshot()
  assert not s['deadline_passed'] and not s['states'].get('admitted')
  subprocess.run([PY,'scripts/ragas_holdout120_revision15.py','--preflight'],cwd=ROOT,check=True)
  with (OUT/'continuation-run-15.log').open('a') as log:
   proc=subprocess.Popen([PY,str(Path(__file__)),'--worker'],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  record={'pid':proc.pid,'submitted_at':time.time(),'mode':'manual then automatic sequential','original_ledger_preserved':True,'no_qa_evaluation':True}
  marker.write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))

if __name__=='__main__':main()
