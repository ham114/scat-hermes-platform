"""Reuse installed RAG-FiT's native local dataset pipeline, with no API stages."""
import runpy
from reranker_ft_common import *
offline()
os.environ.update(HF_DATASETS_OFFLINE='1',HF_DATASETS_CACHE=str(OUT/'datasets-cache'))
if __name__=='__main__':
 assert sys.executable=='/home/h3c/anaconda3/envs/ragfit/bin/python'
 sys.path.insert(0,'/home/h3c/project/python/RAG-FiT')
 sys.argv=['processing.py','--config-dir',str(OUT),'--config-name','processing-reranker','hydra.run.dir='+str(OUT/'ragfit-hydra'),'hydra.job.chdir=false']
 runpy.run_path('/home/h3c/project/python/RAG-FiT/processing.py',run_name='__main__')
