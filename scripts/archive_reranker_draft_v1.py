"""Preserve the pretraining draft before stronger negative screening; no model trained."""
import shutil,time,sys
from reranker_ft_common import *
assert not (OUT/'plan.json').exists() and not any((OUT/s['id']/'training.json').exists()for s in SCHEMES)
tag=sys.argv[1]if len(sys.argv)>1 else 'pre-screen-v1';assert tag in ['pre-screen-v1','pre-screen-v2']
dest=OUT/tag;dest.mkdir(parents=True,exist_ok=True)
names=['draft-plan.json','train.jsonl','dev.jsonl','dev-pools.json','ragfit-pairs.jsonl','negative-review-sample.json','exclusions.json','split-components.json','processing-reranker.yaml','reranker-prompt.txt']
files={}
for name in names:
 p=OUT/name
 if p.exists():shutil.copy2(p,dest/name);files[name]=sha(p)
for name in ['reranker_ft_common.py','prepare_reranker_ft.py']:
 shutil.copy2(ROOT/'scripts'/name,dest/name);files[name]=sha(ROOT/'scripts'/name)
save(dest/'superseded.json',{'at':time.time(),'status':'superseded_before_training','files_sha256':files,'reason':'Manual negative sample review found potentially related UAS/attitude fragments; expand full-text concept screening and exclude pure page-location questions. No formal model training consumed this draft.'})
print(json.dumps({'archived':len(files),'no_formal_training':True}))
