"""Bounded detached jobs for data preparation or the frozen three-run campaign."""
import fcntl,json,subprocess,sys,time
from pathlib import Path
from reranker_ft_common import ROOT,OUT
PY='/home/h3c/anaconda3/envs/scat-rag-train/bin/python'
def main():
 assert sys.executable==PY;phase=sys.argv[1];assert phase in ['prepare','prepare-v2','prepare-v3','validate-and-run','campaign'];OUT.mkdir(parents=True,exist_ok=True)
 with (OUT/(phase+'-launch.lock')).open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);marker=OUT/(phase+'-launch.json')
  if marker.exists():print(json.dumps({'already_submitted':True,**json.loads(marker.read_text())}));return
  target='prepare_reranker_ft.py'if phase.startswith('prepare')else 'validate_and_start_reranker_ft.py'if phase=='validate-and-run'else 'run_reranker_ft_campaign.py'
  with (OUT/(phase+'.log')).open('a')as log:
   p=subprocess.Popen([PY,'scripts/'+target],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  r={'phase':phase,'pid':p.pid,'submitted_at':time.time(),'external_llm_calls':0,'production_changes':0};marker.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':main()
