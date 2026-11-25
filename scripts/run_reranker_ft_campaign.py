"""Complete train + dev/test five-policy evaluation before the next objective."""
import fcntl,subprocess,time
from reranker_ft_common import *
PY='/home/h3c/anaconda3/envs/scat-rag-train/bin/python'
def main():
 assert sys.executable==PY;offline()
 with (OUT/'campaign.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);check_protected();phases=[]
  for scheme in SCHEMES:
   begin=time.time();sid=scheme['id']
   save(OUT/'campaign-progress.json',{'stage':'train','scheme':sid,'priority':scheme['priority']})
   if not (OUT/sid/'training.json').exists():subprocess.run([PY,'scripts/train_reranker_ft.py','--scheme',sid],cwd=ROOT,check=True)
   save(OUT/'campaign-progress.json',{'stage':'evaluate_five_serial_policies','scheme':sid,'priority':scheme['priority']})
   if not (OUT/sid/'eval/summary.json').exists():subprocess.run([PY,'scripts/evaluate_reranker_ft.py','--scheme',sid],cwd=ROOT,check=True)
   check_protected();phases.append({'scheme':sid,'begin':begin,'end':time.time()});save(OUT/'completed-phases.json',phases)
  assert all(phases[i]['end']<=phases[i+1]['begin']for i in range(2))
  save(OUT/'campaign-summary.json',{'status':'completed','serial_training_order':[s['id']for s in SCHEMES],'phases':phases,'settings':SETTINGS,'external_llm_calls':0,'generation_calls':0,'qa_calls':0,'production_changes':0,'protected_unchanged':True})
  print(json.dumps({'campaign_completed':True,'schemes':[s['id']for s in SCHEMES]}),flush=True)
if __name__=='__main__':main()
