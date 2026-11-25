"""Frozen holdout evidence evaluation: local forward scoring, five serial replays."""
from __future__ import annotations
import argparse,collections,datetime,hashlib,json,os,socket,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'src')]
OUT=ROOT/'var/validation/20261125-rag-holdout120-rerank-serial-v1'
FROZEN=ROOT/'var/validation/20261125-ragas-holdout120'
DATA=ROOT/'evaluations/rag_holdout120_candidates_20261125_v1/evaluation.json'
OLD=ROOT/'output/rag-fusion-serial-20261125/weighted2-results.json'
DEPTHS=(5,10,20,50,100)
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',MKL_NUM_THREADS='4',RAGAS_DO_NOT_TRACK='true',LANGCHAIN_TRACING_V2='false',LANGSMITH_TRACING='false')
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):
 h=hashlib.sha256()
 with p.open('rb')as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 return h.hexdigest()
def save(p,x):
 p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.writing');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8');tmp.replace(p)
def stamp():return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()

def aliases_for(cases,records):
 """Supplement only: same source/version/last section and all exact gold quotes."""
 result={}
 for c in cases:
  quotes=collections.defaultdict(set)
  for cl in c['claims']:
   for e in cl['evidence']:quotes[e['chunk_id']].add(e['quote'])
  for cid,qs in quotes.items():
   row=records[cid];key=(row['source_id'],row['source_version'],row['section_path'][-1:])
   result[cid]=[r['chunk_id']for r in records.values()if (r['source_id'],r['source_version'],r['section_path'][-1:])==key and all(q in r['text']for q in qs)]
   assert cid in result[cid]
 return result

def prepare():
 from rerank_postprocess_policies import POLICIES,SOURCE_ALIASES
 from fusion_policy_experiments import POLICIES as FUSION
 from rag_retrieval.runtime import M8_RRF_PROFILE
 history=read(OUT/'historical-context.json');cfg=read(ROOT/'services/rag/qwen3-production-v1.json')
 corpus=Path(cfg['corpus']);vectors=Path(cfg['embedding_cache'])
 assert sha(corpus)==cfg['corpus_sha256'] and sha(vectors)==cfg['embedding_cache_sha256']
 assert sha(ROOT/'scripts/rerank_postprocess_policies.py')==history['policy_sha256']
 assert sha(OLD)==history['weighted2_results_sha256']
 original=read(DATA)['cases'];assert len(original)==120
 frozen=[json.loads(l)for l in (FROZEN/'candidates.jsonl').read_text(encoding='utf8').splitlines()]
 assert sorted(original,key=lambda c:c['slot'])==sorted(frozen,key=lambda c:c['slot'])
 assert sha(FROZEN/'candidates.jsonl')==read(FROZEN/'generation-freeze.json')['candidates_sha256']==read(FROZEN/'audit.json')['candidates_sha256']
 assert all(c['assistant_source_review']['decision']in ['retain','revise_reference']for c in original)
 records={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())}
 cases=[{'id':c['qid'],'cohort':'holdout120','category':c['category'],'question':c['question'],'gold':sorted({e['chunk_id']for cl in c['claims']for e in cl['evidence']}),'groups':c['supporting_evidence_groups']}for c in sorted(original,key=lambda c:c['slot'])]
 prior=[r for r in read(OLD)['cases']if r['cohort']=='old113'];assert len(prior)==113
 cases += [{k:r[k]for k in ['id','cohort','category','question','gold','groups']}for r in prior]
 assert len({c['id']for c in cases})==233 and all(len(c['gold'])==2 for c in cases if c['cohort']=='holdout120')
 data={'cases':cases,'cohorts':{'holdout120':120,'old113':113},'metric':'primary strict frozen chunk IDs; not answer accuracy'}
 quoted=aliases_for(original,records)
 protected=[DATA,FROZEN/'candidates.jsonl',FROZEN/'generation-freeze.json',FROZEN/'assistant-quality-notes.json',FROZEN/'calls.sqlite',OLD,corpus,vectors,
 ROOT/'services/rag/qwen3-production-v1.json',ROOT/'services/rag/real-agent-production-v1.json',ROOT/cfg['model_manifest'],
 *[ROOT/'src/rag_retrieval'/n for n in ['baseline.py','runtime.py','production.py','enhancements.py','rerank_comparison.py']],
 *[ROOT/'scripts'/n for n in ['rerank_postprocess_policies.py','fusion_policy_experiments.py','evidence_retrieval_metrics.py','run_holdout_rerank_serial.py']],OUT/'historical-context.json']
 plan={'schema_version':1,'policies':POLICIES,'serial_order':[p['id']for p in POLICIES],'source_aliases':SOURCE_ALIASES,
 'fusion_policy':next(p for p in FUSION if p['id']=='weighted2'),'retrieval_profile':M8_RRF_PROFILE,'candidate_limit':100,'leaf_limit':200,
 'cohorts':data['cohorts'],'protected_sha256':{p.relative_to(ROOT).as_posix():sha(p)for p in protected},
 'original_bm25_sha256':history['original_bm25_sha256'],'frozen_candidates_sha256':sha(FROZEN/'candidates.jsonl'),
 'shared_scoring':'Fresh BGE queries and one existing local Qwen3 reranker forward score per holdout query; same100 candidates and scores for all five policies. Existing old113 scores replayed only.',
 'serial_execution':'Complete every case for one policy before beginning next policy; shared inference is cached once before policy runs.',
 'no_label_inputs':'Ranking accepts only question, candidates, existing local relevance scores, source metadata and cache similarities. No reference answers, gold IDs, cohorts or case IDs.',
 'selection_rule':'Do not tune. Compare holdout Top5 thenTop10; identify old113 Top5/10 per-case losses separately. Keep independent-test winner distinct from prior development winner.',
 'primary_metric':'Strict two-chunk complete evidence coverage at5/10/20/50/100, evidence recall and per-case ranks.',
 'supplementary_metric':'Frozen quote-compatible alternatives, same source/version/last section with every marked quote contained; a diagnostic proxy only, never replaces strict IDs.',
 'qa_calls':0,'external_llm_calls':0,'generation_calls':0,'production_changes':0,'network_policy':'all outbound socket connections denied in process'}
 assert sha(ROOT/'src/rag_retrieval/baseline.py')==plan['original_bm25_sha256']
 for name,obj in [('dataset.json',data),('quote-compatible-alternatives.json',quoted),('preregistration.json',plan)]:
  path=OUT/name
  if path.exists():assert read(path)==obj,'frozen experiment changed'
  else:save(path,obj)
 print(json.dumps({'prepared':True,'cohorts':data['cohorts'],'policies':plan['serial_order'],'quote_alias_gold_contexts':sum(len(v)>1 for v in quoted.values()),'external_calls':0}),flush=True)

def aggregate(rows,field):
 return {h:{str(k):{'cases':len(rs),'complete_cases':sum(r[field][str(k)]['all_evidence']for r in rs),'evidence_hits':sum(r[field][str(k)]['evidence_hits']for r in rs),'evidence_total':sum(len(r['gold'])for r in rs)}for k in DEPTHS}for h in ['holdout120','old113']for rs in [[r for r in rows if r['cohort']==h]]}

def run():
 import fcntl
 with (OUT/'run.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  if (OUT/'summary.json').exists():print(json.dumps({'already_completed':True}));return
  plan=read(OUT/'preregistration.json');data=read(OUT/'dataset.json')['cases'];history=read(OUT/'historical-context.json')
  assert all(sha(ROOT/p)==h for p,h in plan['protected_sha256'].items()),'protected file drift'
  from rerank_postprocess_policies import POLICIES,rerank
  from evidence_retrieval_metrics import coverage
  from fusion_policy_experiments import select
  import numpy as np
  assert POLICIES==plan['policies']
  identity={'plan_sha256':sha(OUT/'preregistration.json'),'dataset_sha256':sha(OUT/'dataset.json'),'alias_sha256':sha(OUT/'quote-compatible-alternatives.json')}
  p=OUT/'run-identity.json'
  if p.exists():assert read(p)==identity
  else:save(p,identity)
  cfg=read(ROOT/'services/rag/qwen3-production-v1.json');rows=list(map(json.loads,Path(cfg['corpus']).read_text(encoding='utf8').splitlines()));records={r['chunk_id']:r for r in rows};ids=[r['chunk_id']for r in rows]
  vectors=np.load(cfg['embedding_cache'],mmap_mode='r');assert vectors.shape==(8919,1024)and np.isfinite(vectors).all()and np.allclose(np.linalg.norm(vectors,axis=1),1,atol=1e-3)
  position={cid:i for i,cid in enumerate(ids)};prior={r['id']:r for r in read(OLD)['cases']if r['cohort']=='old113'}
  quoted=read(OUT/'quote-compatible-alternatives.json')
  missing=[u for u in data if u['cohort']=='holdout120' and not (OUT/'shared'/(u['id']+'.json')).exists()]
  engine=None;smoke_pairs=0;generate_attempts=[]
  if missing:
   import torch
   from transformers.generation.utils import GenerationMixin
   def no_generate(*a,**kw):generate_attempts.append('blocked');raise RuntimeError('generation_disabled_in_evidence_evaluation')
   GenerationMixin.generate=no_generate
   from rag_retrieval.production import Retriever,load_config
   from rag_retrieval.enhancements import rank_exact_identifier_matches
   engine=Retriever(ROOT,load_config(ROOT,'services/rag/qwen3-production-v1.json'));smoke_pairs=2
   assert sum(v<0 for v in engine.index.bm25._idf.values())==37
  shared={};started=time.time()
  for number,u in enumerate(data,1):
   path=OUT/'shared'/(u['id']+'.json')
   if path.exists():
    r=read(path);assert r['run_identity']==identity
   elif u['cohort']=='old113':
    old=prior[u['id']];assert len(old['pool'])==100 and old['scores']
    r={**u,'pool':old['pool'],'scores':old['scores'],'origin':'saved_same_model_weighted2_old113','run_identity':identity,'scoring_seconds':None}
    assert sorted(r['pool'],key=lambda c:(-r['scores'][c],r['pool'].index(c)))==old['ranking']
    save(path,r)
   else:
    index=engine.index;q=' '.join(u['question'].split());t=time.perf_counter()
    v=np.asarray(index.model.encode([q],normalize_embeddings=True,show_progress_bar=False)[0],dtype=np.float32)
    dense=[index.chunk_ids[i]for i in np.argsort(-(index.corpus_vectors@v),kind='stable')]
    lexical=[c for c,_ in index.bm25.rank(q,limit=len(ids))]
    exact=[c for c,_ in rank_exact_identifier_matches(q,index.chunks,limit=200)]
    retrieval_seconds=time.perf_counter()-t
    pool=select(plan['fusion_policy'],dense[:200],lexical[:200],exact,index.chunks_by_id,q,plan['retrieval_profile']);assert len(pool)==100
    torch.cuda.synchronize();t=time.perf_counter();values,info=engine.reranker.score(u['question'].strip(),[index.chunks_by_id[c].retrieval_text()for c in pool]);torch.cuda.synchronize();seconds=time.perf_counter()-t
    assert not info['truncated_pairs'] and len(values)==100 and all(np.isfinite(values))
    branches={'bge':dense[:200],'bm25':lexical[:200],'exact':exact,'candidate100':pool}
    r={**u,'pool':pool,'scores':dict(zip(pool,values,strict=True)),'origin':'fresh_local_retrieval_and_reranker_forward','run_identity':identity,'branches':branches,
     'branch_metrics':{n:coverage(v,u['gold'],u['groups'])for n,v in branches.items()},'gold_leaf_ranks':{c:{'bge':dense.index(c)+1,'bm25':lexical.index(c)+1 if c in lexical else None,'exact':exact.index(c)+1 if c in exact else None,'candidate100':pool.index(c)+1 if c in pool else None}for c in u['gold']},
     'retrieval_seconds':retrieval_seconds,'scoring_seconds':seconds,'rerank_info':info}
    save(path,r)
   shared[u['id']]=r
   save(OUT/'progress.json',{'stage':'shared_local_scoring','completed':number,'total':233,'holdout_scored':min(number,120),'qa_calls':0,'generation_calls':0,'external_llm_calls':0})
   if number%10==0 and number<=120:print(json.dumps({'stage':'local_scoring','completed':number,'total':120}),flush=True)
  baseline={};summaries=[];serial=[];simtime=[]
  sims={}
  for u in data:
   r=shared[u['id']];base=sorted(r['pool'],key=lambda c:(-r['scores'][c],r['pool'].index(c)));t=time.perf_counter();v=np.asarray(vectors[[position[c]for c in base]],dtype=np.float64);sims[u['id']]=np.clip(v@v.T,-1,1);simtime.append(time.perf_counter()-t)
  for policy in POLICIES:
   begin=time.time();cases=[]
   for number,u in enumerate(data,1):
    r=shared[u['id']];t=time.perf_counter();ranked,activation=rerank(policy,query=u['question'],pool=r['pool'],scores=r['scores'],records=records,similarities=sims[u['id']]);seconds=time.perf_counter()-t
    assert len(ranked)==len(set(ranked))==100 and set(ranked)==set(r['pool'])
    metrics=coverage(ranked,u['gold'],u['groups']);supp={}
    if u['cohort']=='holdout120':
     for k in DEPTHS:
      found=set(ranked[:k]);hits=sum(bool(found&set(quoted[c]))for c in u['gold']);supp[str(k)]={'evidence_hits':hits,'all_evidence':hits==len(u['gold'])}
    cases.append({**u,'ranking':ranked,'metrics':metrics,'quote_compatible_metrics':supp,'gold_ranks':{c:ranked.index(c)+1 if c in ranked else None for c in u['gold']},'seconds_postprocess':seconds,'activation':activation})
    save(OUT/'progress.json',{'stage':'serial_policy','policy':policy['id'],'completed':number,'total':233,'qa_calls':0,'generation_calls':0,'external_llm_calls':0})
   if not baseline:baseline={r['id']:r for r in cases}
   paired={h:{str(k):{'gained':[r['id']for r in cases if r['cohort']==h and r['metrics'][str(k)]['all_evidence']and not baseline[r['id']]['metrics'][str(k)]['all_evidence']],
    'lost':[r['id']for r in cases if r['cohort']==h and not r['metrics'][str(k)]['all_evidence']and baseline[r['id']]['metrics'][str(k)]['all_evidence']]}for k in (5,10)}for h in ['holdout120','old113']}
   metrics=aggregate(cases,'metrics');old=next(p for p in history['policies']if p['policy']['id']==policy['id'])
   assert metrics['old113']==old['metrics']['old113'] and paired['old113']==old['paired_against_reranker']['old113'],'historical control replay mismatch'
   summary={'policy':policy,'metrics':metrics,'paired_against_reranker':paired,'quote_compatible_holdout':{str(k):{'complete_cases':sum(r['quote_compatible_metrics'][str(k)]['all_evidence']for r in cases if r['cohort']=='holdout120'),'evidence_hits':sum(r['quote_compatible_metrics'][str(k)]['evidence_hits']for r in cases if r['cohort']=='holdout120')}for k in DEPTHS},
    'postprocess_p50_ms':float(np.percentile([r['seconds_postprocess']for r in cases if r['cohort']=='holdout120'],50)*1000),'postprocess_p95_ms':float(np.percentile([r['seconds_postprocess']for r in cases if r['cohort']=='holdout120'],95)*1000),
    'explicit_source_rule_active_holdout':sum(bool(r['activation']['reserved_source_candidates'])for r in cases if r['cohort']=='holdout120'),'serial_started_at':begin,'serial_completed_at':time.time()}
   save(OUT/(policy['id']+'-results.json'),{'cases':cases});save(OUT/(policy['id']+'-summary.json'),summary);summaries.append(summary);serial.append(policy['id'])
   print(json.dumps({'policy_completed':policy['id'],'holdout':metrics['holdout120'],'old_control_parity':True}),flush=True)
  assert all(summaries[i]['serial_completed_at']<=summaries[i+1]['serial_started_at']for i in range(4))
  assert all(sha(ROOT/p)==h for p,h in plan['protected_sha256'].items()) and not generate_attempts
  assert not any(m in sys.modules for m in ['openai','anthropic','ragas'])
  latency=[shared[u['id']]['scoring_seconds']for u in data if u['cohort']=='holdout120']
  stages={n:{str(k):{'complete_cases':sum(shared[u['id']]['branch_metrics'][n][str(k)]['all_evidence']for u in data if u['cohort']=='holdout120'),'evidence_hits':sum(shared[u['id']]['branch_metrics'][n][str(k)]['evidence_hits']for u in data if u['cohort']=='holdout120'),'cases':120,'evidence_total':240}for k in DEPTHS}for n in ['bge','bm25','exact','candidate100']}
  save(OUT/'summary.json',{'status':'completed','completed_at':stamp(),'serial_order':serial,'cases_per_policy':233,'holdout_cases':120,'cached_old_controls':113,'policies':summaries,'recall_stages':stages,
   'fresh_reranker_pairs_in_this_invocation':len(missing)*100+smoke_pairs,'fresh_bge_queries_in_this_invocation':len(missing),'fresh_reranker_pairs_for_holdout':12000,
   'shared_reranker_p50_seconds':float(np.percentile(latency,50)),'shared_reranker_p95_seconds':float(np.percentile(latency,95)),
   'shared_similarity_p50_ms':float(np.percentile(simtime,50)*1000),'seconds_excluding_model_loading':time.time()-started,
   'qa_calls':0,'generation_calls':0,'external_llm_calls':0,'production_changes':0,'network_connections_attempted':0,'protected_files_unchanged':True,'historical_old113_controls_reproduced':True,'run_identity':identity})

def main():
 attempts=[]
 def deny(*a,**k):attempts.append(True);raise RuntimeError('network_disabled_for_holdout_evidence_test')
 socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
 a=argparse.ArgumentParser();a.add_argument('action',choices=['prepare','run']);args=a.parse_args();OUT.mkdir(parents=True,exist_ok=True)
 if args.action=='prepare':prepare()
 else:run()
 assert not attempts,'unexpected network attempt'
if __name__=='__main__':main()
