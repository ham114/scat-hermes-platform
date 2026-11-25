"""Detach exactly one evidence-only evaluation; never duplicate a submitted job."""
import fcntl,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-rag-holdout120-rerank-serial-v1'
PY='/home/h3c/anaconda3/envs/scat-rag-train/bin/python'
def main():
 assert sys.executable==PY;OUT.mkdir(parents=True,exist_ok=True)
 with (OUT/'launch.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);marker=OUT/'launch.json'
  if marker.exists():print(json.dumps({'already_submitted':True,**json.loads(marker.read_text())}));return
  subprocess.run([PY,'scripts/run_holdout_rerank_serial.py','prepare'],cwd=ROOT,check=True)
  with (OUT/'run.log').open('a')as stream:
   p=subprocess.Popen([PY,'scripts/run_holdout_rerank_serial.py','run'],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
  r={'pid':p.pid,'submitted_at':time.time(),'fresh_holdout_queries':120,'serial_policy_count':5,'old113_scoring':'cached replay only','qa_calls':0,'generation_calls':0,'external_llm_calls':0,'production_changes':0}
  marker.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':main()
