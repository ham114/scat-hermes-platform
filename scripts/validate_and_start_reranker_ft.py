"""Run pretraining validations, freeze once, then execute the authorized serial campaign."""
import fcntl,subprocess,time
from reranker_ft_common import *
PY='/home/h3c/anaconda3/envs/scat-rag-train/bin/python'
RAGFIT='/home/h3c/anaconda3/envs/ragfit/bin/python'
def call(cmd):subprocess.run(cmd,cwd=ROOT,check=True)
def main():
 offline()
 with (OUT/'validation.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  if not (OUT/'plan.json').exists():
   with (OUT/'unittest.log').open('w')as log:
    subprocess.run([PY,'tests/rag_retrieval/test_reranker_ft_contracts.py'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
   assert 'Ran 13 tests'in (OUT/'unittest.log').read_text()
   call([RAGFIT,'scripts/run_ragfit_reranker_processing.py'])
   if not (OUT/'smoke-v3/training.json').exists():call([PY,'scripts/train_reranker_ft.py','--smoke','--smoke-tag','smoke-v3'])
   if not (OUT/'smoke-v3/eval/summary.json').exists():call([PY,'scripts/evaluate_reranker_ft.py','--scheme','smoke-v3','--smoke'])
   save(OUT/'preflight-validation.json',{'status':'passed','unit_tests':13,'test_log_sha256':sha(OUT/'unittest.log'),'smoke':'smoke-v3','smoke_training_sha256':sha(OUT/'smoke-v3/training.json'),'smoke_evaluation_sha256':sha(OUT/'smoke-v3/eval/summary.json'),'external_llm_calls':0,'generation_calls':0})
   call([PY,'scripts/freeze_reranker_ft.py'])
  call([PY,'scripts/run_reranker_ft_campaign.py'])
if __name__=='__main__':main()
