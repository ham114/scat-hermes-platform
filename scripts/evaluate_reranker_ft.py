"""Fresh adapter scoring on frozen dev/test pools, then five serial postprocessings."""
import argparse,collections,contextlib,time
from reranker_ft_common import *
from train_reranker_ft import load_scorer

def main():
 p=argparse.ArgumentParser();p.add_argument('--scheme',required=True);p.add_argument('--smoke',action='store_true');a=p.parse_args();offline()
 import numpy as np
 from rerank_postprocess_policies import POLICIES,rerank
 from evidence_retrieval_metrics import coverage
 if not a.smoke:check_protected()
 dest=OUT/a.scheme/'eval';dest.mkdir(parents=True,exist_ok=True)
 assert not (dest/'summary.json').exists(),'completed adapter evaluation preserved'
 trained=read(OUT/a.scheme/'training.json');adapter=Path(trained['adapter_path'])
 assert all(sha(adapter/n)==h for n,h in trained['adapter_files_sha256'].items())
 identity={'adapter_files':trained['adapter_files_sha256'],'dev_pools':sha(OUT/'dev-pools.json'),'test_pools':None if a.smoke else sha(OUT/'test-pools.json')}
 cfg,chunks,rs=corpus();vectors=np.load(cfg['embedding_cache'],mmap_mode='r');positions={c:i for i,c in enumerate(rs)}
 cases=read(OUT/'dev-pools.json')[:2]if a.smoke else read(OUT/'dev-pools.json')+read(OUT/'test-pools.json')
 r=load_scorer(adapter);scored={};baseline_dev={};started=time.time()
 for j,u in enumerate(cases,1):
  cid=u['id'];path=dest/'scores'/(cid+'.json');pool=u['pool'];assert len(pool)==len(set(pool))==100
  if path.exists():entry=read(path);assert entry['identity']==identity and entry['pool']==pool
  else:
   t=time.perf_counter();values,info=r.score(u['question'].strip(),[chunks[c].retrieval_text()for c in pool]);sec=time.perf_counter()-t
   assert not info['truncated_pairs'] and all(np.isfinite(values))
   entry={**u,'scores':dict(zip(pool,values,strict=True)),'seconds':sec,'info':info,'identity':identity};save(path,entry)
  scored[cid]=entry
  if u['cohort']=='dev':
   path=OUT/'dev-base-scores'/(cid+'.json');base_id={'pool':pool,'question':u['question'],'manifest':cfg['model_manifest_sha256']}
   if path.exists():base=read(path);assert base['identity']==base_id
   else:
    with r.model.disable_adapter():values,info=r.score(u['question'].strip(),[chunks[c].retrieval_text()for c in pool])
    assert not info['truncated_pairs'];base={'identity':base_id,'scores':dict(zip(pool,values,strict=True))};save(path,base)
   baseline_dev[cid]=base
  save(dest/'progress.json',{'stage':'adapter_scoring','scheme':a.scheme,'completed':j,'total':len(cases),'cohort':u['cohort'],'generation_calls':0,'external_llm_calls':0})
  if j%20==0:print(json.dumps({'scheme':a.scheme,'scored':j,'total':len(cases)}),flush=True)
 summaries=[];base_ranker={};intervals=[];cohorts=sorted({u['cohort']for u in cases})
 for policy in POLICIES:
  begin=time.time();rows=[];before={}
  if not a.smoke:
   before.update({u['id']:u for u in read(BASE_EVAL/(policy['id']+'-results.json'))['cases']})
  for u in cases:
   entry=scored[u['id']];pool=u['pool'];scores=entry['scores'];order={c:i for i,c in enumerate(pool)};base=sorted(pool,key=lambda c:(-scores[c],order[c]));v=np.asarray(vectors[[positions[c]for c in base]],dtype=np.float64);sim=np.clip(v@v.T,-1,1)
   ranked,activation=rerank(policy,query=u['question'],pool=pool,scores=scores,records=rs,similarities=sim)
   assert len(ranked)==100 and set(ranked)==set(pool)
   row={**u,'ranking':ranked,'metrics':coverage(ranked,u['gold'],u['groups']),'activation':activation,'gold_ranks':{c:ranked.index(c)+1 if c in ranked else None for c in u['gold']}}
   rows.append(row)
   if u['cohort']=='dev':
    b=baseline_dev[u['id']]['scores'];base=sorted(pool,key=lambda c:(-b[c],order[c]));v=np.asarray(vectors[[positions[c]for c in base]],dtype=np.float64);sim=np.clip(v@v.T,-1,1)
    br,_=rerank(policy,query=u['question'],pool=pool,scores=b,records=rs,similarities=sim)
    before[u['id']]={**u,'metrics':coverage(br,u['gold'],u['groups'])}
  if policy['id']=='reranker':base_ranker={r['id']:r for r in rows}
  metrics={h:{str(k):{'complete_cases':sum(x['metrics'][str(k)]['all_evidence']for x in xs),'cases':len(xs),'evidence_hits':sum(x['metrics'][str(k)]['evidence_hits']for x in xs),'evidence_total':sum(len(x['gold'])for x in xs)}for k in [5,10,20,50,100]}for h in cohorts for xs in [[x for x in rows if x['cohort']==h]]}
  base_metrics={h:{str(k):{'complete_cases':sum(before[x['id']]['metrics'][str(k)]['all_evidence']for x in xs),'cases':len(xs)}for k in [5,10,20,50,100]}for h in cohorts for xs in [[x for x in rows if x['cohort']==h]]}
  paired={h:{str(k):{'gained':[x['id']for x in rows if x['cohort']==h and x['metrics'][str(k)]['all_evidence']and not before[x['id']]['metrics'][str(k)]['all_evidence']],
    'lost':[x['id']for x in rows if x['cohort']==h and not x['metrics'][str(k)]['all_evidence']and before[x['id']]['metrics'][str(k)]['all_evidence']]}for k in [5,10]}for h in cohorts}
  same_model_paired={h:{str(k):{'gained':[x['id']for x in rows if x['cohort']==h and x['metrics'][str(k)]['all_evidence']and not base_ranker[x['id']]['metrics'][str(k)]['all_evidence']],
    'lost':[x['id']for x in rows if x['cohort']==h and not x['metrics'][str(k)]['all_evidence']and base_ranker[x['id']]['metrics'][str(k)]['all_evidence']]}for k in [5,10]}for h in cohorts}
  for row in rows:
   if row['cohort']!='dev':assert row['metrics']['100']==before[row['id']]['metrics']['100'],'candidate universe changed'
  end=time.time();intervals.append((begin,end));summary={'policy':policy,'metrics':metrics,'untrained_same_policy_metrics':base_metrics,'paired_against_untrained_same_policy':paired,'paired_against_adapted_reranker':same_model_paired,'serial_begin':begin,'serial_end':end}
  save(dest/(policy['id']+'-results.json'),{'cases':rows});save(dest/(policy['id']+'-summary.json'),summary);summaries.append(summary)
  print(json.dumps({'scheme':a.scheme,'policy_completed':policy['id'],'metrics':metrics}),flush=True)
 assert all(intervals[i][1]<=intervals[i+1][0]for i in range(4))
 if not a.smoke:check_protected()
 dev_raw=summaries[0];eligible=all(dev_raw['metrics']['dev'][str(k)]['complete_cases']>=dev_raw['untrained_same_policy_metrics']['dev'][str(k)]['complete_cases']for k in [5,10])
 save(dest/'summary.json',{'status':'completed','scheme':a.scheme,'cohorts':dict(collections.Counter(u['cohort']for u in cases)),'policies':summaries,'serial_order':[p['id']for p in POLICIES],'dev_raw_top5_top10_not_worse':eligible,'test_used_for_checkpoint_selection':False,'checkpoint':'fixed_epoch1_adapter_last','seconds':time.time()-started,'fresh_forward_pairs':len(cases)*100,'identity':identity,'generation_calls':0,'external_llm_calls':0,'production_changes':0})
if __name__=='__main__':main()
