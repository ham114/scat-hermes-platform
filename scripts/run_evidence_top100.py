"""Frozen 120-question evidence-only stage trace. No answer/judge/API clients."""
import collections,datetime,hashlib,json,os,socket,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
OUT=ROOT/'var/validation/20261125-rag-evidence-top100';DEST=ROOT/'output/rag-evidence-top100-20261125'
DATA=ROOT/'output/ragas-multihop120-evaluation-20261124/dataset.json'
BASE=ROOT/'output/ragas-multihop120-evaluation-20261124/retrieval-summary.json'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',RAGAS_DO_NOT_TRACK='true',LANGCHAIN_TRACING_V2='false',LANGSMITH_TRACING='false',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4')
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
 return h.hexdigest()
def save(p,v):
 p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.writing');t.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8');t.replace(p)
def block_network():
 attempts=[]
 def deny(*args,**kwargs):attempts.append('blocked');raise RuntimeError('network_disabled_for_evidence_only_run')
 socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny
 return attempts
def main():
 assert not (OUT/'summary.json').exists(),'completed diagnostic preserved'
 OUT.mkdir(parents=True,exist_ok=True);DEST.mkdir(parents=True,exist_ok=True)
 import fcntl
 with (OUT/'run.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  run()
def run():
 attempts=block_network()
 import numpy as np
 from rag_retrieval.production import Retriever,load_config
 from rag_retrieval.baseline import reciprocal_rank_fusion
 from rag_retrieval.enhancements import apply_source_boost,infer_source_ids,rank_exact_identifier_matches
 from rag_retrieval.rerank_comparison import frozen_candidates,order_by_scores
 from rag_retrieval.runtime import M8_RRF_PROFILE
 from evidence_retrieval_metrics import rank_map,coverage,loss_stage
 started=time.time();cfg=load_config(ROOT,'services/rag/qwen3-production-v1.json');data=read(DATA)
 assert data['corpus_sha256']==cfg['corpus_sha256']=='bd2010dab0c5b4ad3586b85c53497847d0a896a9ca09e638fca3a14219ff6aa3'
 assert len(data['units'])==120 and cfg['candidate_limit']==100
 protected_paths=[DATA,BASE,ROOT/'services/rag/qwen3-production-v1.json',ROOT/'services/rag/real-agent-production-v1.json',ROOT/'var/validation/20261124-rag-multihop120-eval/qa-ledger.sqlite',ROOT/'var/validation/20261124-ragas-multihop120/calls.sqlite']
 protected={str(p.relative_to(ROOT)):sha(p) for p in protected_paths}
 sources=['scripts/run_evidence_top100.py','scripts/evidence_retrieval_metrics.py','src/rag_retrieval/production.py','src/rag_retrieval/runtime.py','src/rag_retrieval/baseline.py','src/rag_retrieval/enhancements.py','src/rag_retrieval/rerank_comparison.py']
 plan={'cases':120,'depths':[5,10,20,50,100],'leaf_recall_depth':200,'production_rerank_pool':100,'dataset_sha256':sha(DATA),'corpus_sha256':cfg['corpus_sha256'],'embedding_cache_sha256':cfg['embedding_cache_sha256'],
  'profile':M8_RRF_PROFILE,'source_sha256':{s:sha(ROOT/s) for s in sources},'protected_files':protected,'qa_calls':0,'external_llm_calls':0,
  'protocol':'Original frozen questions only; local BGE dense, BM25, exact identifiers, RRF200, source boost, candidate100, local Qwen relevance reranking. No answer generation, no Ragas or external judgment. Network denied inside this process.',
  'primary_metric':'Strict frozen chunk-ID evidence recall and complete-two-chunk rate. Quote-equivalent alternatives reported separately with deterministic same-source/version containment.'}
 if (OUT/'plan.json').exists():assert read(OUT/'plan.json')==plan
 else:save(OUT/'plan.json',plan)
 save(OUT/'progress.json',{'status':'loading_local_models','completed':0,'total':120})
 engine=Retriever(ROOT,cfg);index=engine.index;ids=index.chunk_ids;byid=engine.records;id_index={cid:i for i,cid in enumerate(ids)};vectors=index.corpus_vectors;model=index.model
 assert np.isfinite(vectors).all();norms=np.linalg.norm(vectors,axis=1);assert np.allclose(norms,1,atol=1e-3)
 positive_ids=sorted({cid for u in data['units'] for cid in u['gold_chunk_ids']})
 sample_ids=positive_ids+list(dict.fromkeys(ids[i] for i in np.linspace(0,len(ids)-1,32,dtype=int) if ids[i] not in positive_ids))
 texts=[index.chunks_by_id[c].retrieval_text() for c in sample_ids]
 fresh=model.encode(texts,batch_size=4,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=False)
 cached=vectors[[id_index[c] for c in sample_ids]];cosines=np.sum(fresh*cached,axis=1)
 token_lengths=[len(x) for x in model.tokenizer(texts,add_special_tokens=True,padding=False,truncation=False)['input_ids']]
 audit={'embedding_model':cfg['embedding_model'],'dimensions':int(vectors.shape[1]),'cache_rows':len(ids),'norm_min':float(norms.min()),'norm_max':float(norms.max()),
  'positive_unique_chunks':len(positive_ids),'reencoded_cache_rows':len(sample_ids),'all_positive_rows_reencoded':True,'cache_vs_reencoded_min_cosine':float(cosines.min()),'cache_vs_reencoded_mean_cosine':float(cosines.mean()),
  'max_seq_length':model.max_seq_length,'max_audited_document_tokens':max(token_lengths),'truncated_audited_documents':sum(n>model.max_seq_length for n in token_lengths),
  'cache_reencoding_details':[{'chunk_id':c,'cosine':float(s),'tokens':n} for c,s,n in zip(sample_ids,cosines,token_lengths)]}
 save(OUT/'embedding-audit.json',audit);assert float(cosines.min())>.999,'positive embedding/cache mismatch; diagnose before blaming BGE'
 baseline={r['case_id']:r for r in read(BASE)['cases']};rows=[]
 class FixedQuery:
  def encode(self,sentences,**kwargs):assert sentences==[normalized];return np.array([query_vector])
 canonical=lambda t:' '.join(t.split())
 source_rows=collections.defaultdict(list)
 for r in byid.values():source_rows[(r['source_id'],r['source_version'])].append((r['chunk_id'],canonical(r['text'])))
 for number,u in enumerate(data['units'],1):
  path=OUT/'cases'/(u['case_id']+'.json')
  if path.exists():
   r=read(path);assert r['question']==u['question'] and r['dataset_sha256']==plan['dataset_sha256'];rows.append(r);continue
  before=time.monotonic();query=u['question'].strip();normalized=' '.join(query.split());query_vector=np.asarray(model.encode([normalized],normalize_embeddings=True,show_progress_bar=False)[0],dtype=np.float32)
  dense_scores=vectors@query_vector;dense_order=np.argsort(-dense_scores,kind='stable');dense=[ids[i] for i in dense_order]
  bm25_pairs=index.bm25.rank(normalized,limit=len(ids));bm25=[cid for cid,_ in bm25_pairs]
  exact_pairs=rank_exact_identifier_matches(normalized,index.chunks,limit=len(ids));exact=[cid for cid,_ in exact_pairs]
  branches=[bm25[:200],dense[:200]];weights=[1.,1.]
  if exact:branches.append(exact[:200]);weights.append(float(M8_RRF_PROFILE['exact_weight']))
  fusion=reciprocal_rank_fusion(branches,rank_constant=int(M8_RRF_PROFILE['rrf_k']),weights=weights,limit=None)
  boosted=apply_source_boost(fusion[:200],index.chunks_by_id,infer_source_ids(normalized),float(M8_RRF_PROFILE['source_multiplier']))
  rrf_ids=[cid for cid,_ in fusion];boost_ids=[cid for cid,_ in boosted];pool=boost_ids[:100]
  index.model=FixedQuery()
  try:assert pool==frozen_candidates(index,query,100)
  finally:index.model=model
  scores,info=engine.reranker.score(query,[index.chunks_by_id[cid].retrieval_text() for cid in pool]);assert not info['truncated_pairs']
  ranked=order_by_scores(pool,scores);assert len(ranked)==100 and set(ranked)==set(pool)
  stages={'dense':dense,'bm25':bm25,'exact':exact,'rrf_union':rrf_ids,'rrf200':[cid for cid,_ in fusion[:200]],'boosted':boost_ids,'candidate100':pool,'reranked':ranked}
  maps={name:rank_map(values) for name,values in stages.items()};bms=dict(bm25_pairs);rrs=dict(zip(pool,scores));gold=[]
  for cid in u['gold_chunk_ids']:
   ranks={name:m.get(cid) for name,m in maps.items()};r=byid[cid]
   quotes=list(dict.fromkeys(e['quote'] for e in u['gold_evidence'] if e['chunk_id']==cid));needles=[canonical(q) for q in quotes]
   assert needles and all(n in canonical(r['text']) for n in needles)
   alternatives=[other for other,text in source_rows[(r['source_id'],r['source_version'])] if all(n in text for n in needles)]
   assert cid in alternatives
   gold.append({'chunk_id':cid,'source_id':r['source_id'],'section_path':r['section_path'],'locator':r['locator'],'quotes':quotes,'ranks':ranks,'dense_cosine':float(dense_scores[id_index[cid]]),'bm25_score':bms.get(cid,0.),'reranker_score':rrs.get(cid),
    'loss_at_top5':loss_stage(ranks),'loss_at_top10':loss_stage(ranks,delivery_depth=10),'dense_beyond_100':ranks['dense']>100,'dense_beyond_200':ranks['dense']>200,'quote_equivalent_chunk_ids':alternatives})
  measures={name:coverage(ranking,u['gold_chunk_ids'],u['supporting_evidence_groups']) for name,ranking in stages.items()}
  alternatives={}
  for name,ranking in stages.items():
   maps_here=maps[name];witness=[min((maps_here[c] for c in g['quote_equivalent_chunk_ids'] if c in maps_here),default=None) for g in gold]
   alternatives[name]={str(k):{'evidence_hits':sum(n is not None and n<=k for n in witness),'all_evidence':all(n is not None and n<=k for n in witness)} for k in [5,10,20,50,100]}
  r={'case_id':u['case_id'],'question':u['question'],'category':u['category'],'require_cross_source':u['require_cross_source'],'language':u['language'],'dataset_sha256':plan['dataset_sha256'],'gold':gold,
   'rankings':{name:[{'chunk_id':cid,'rank':i+1} for i,cid in enumerate(ranking[:200])] for name,ranking in stages.items()},'metrics':measures,'quote_equivalent_metrics':alternatives,
   'inferred_source_boost_ids':list(infer_source_ids(normalized)),'reranker_input_info':info,'query_tokens':len(model.tokenizer.encode(normalized,add_special_tokens=True)),
   'production_top10_parity':ranked[:10]==[e['chunk_id'] for e in baseline[u['case_id']]['ranked'][:10]],'elapsed_seconds':time.monotonic()-before}
  save(path,r);rows.append(r);save(OUT/'progress.json',{'status':'running','completed':number,'total':120,'external_llm_calls':0,'qa_calls':0});print(json.dumps({'completed':number,'case':u['case_id']}),flush=True)
 assert protected=={str(p.relative_to(ROOT)):sha(p) for p in protected_paths}
 assert not attempts,'unexpected network attempt'
 summary={'status':'completed','cases':120,'evidence_instances':sum(len(r['gold']) for r in rows),'depths':[5,10,20,50,100],
  'by_stage':{name:{str(k):{'complete_cases':sum(r['metrics'][name][str(k)]['all_evidence'] for r in rows),'any_cases':sum(r['metrics'][name][str(k)]['any_evidence'] for r in rows),
   'evidence_hits':sum(r['metrics'][name][str(k)]['evidence_hits'] for r in rows),'evidence_recall':sum(r['metrics'][name][str(k)]['evidence_hits'] for r in rows)/240} for k in [5,10,20,50,100]} for name in stages},
  'quote_equivalent_by_stage':{name:{str(k):{'complete_cases':sum(r['quote_equivalent_metrics'][name][str(k)]['all_evidence'] for r in rows),'evidence_hits':sum(r['quote_equivalent_metrics'][name][str(k)]['evidence_hits'] for r in rows)} for k in [5,10,20,50,100]} for name in stages},
  'loss_counts_top5':dict(collections.Counter(g['loss_at_top5'] for r in rows for g in r['gold'])),'loss_counts_top10':dict(collections.Counter(g['loss_at_top10'] for r in rows for g in r['gold'])),
  'production_top10_parity_cases':sum(r['production_top10_parity'] for r in rows),'candidate100_equals_rerank100_all_cases':all(set(e['chunk_id'] for e in r['rankings']['candidate100'])==set(e['chunk_id'] for e in r['rankings']['reranked']) for r in rows),
  'query_truncation_count':sum(r['query_tokens']>model.max_seq_length for r in rows),'truncated_reranker_pairs':sum(r['reranker_input_info']['truncated_pairs'] for r in rows),
  'embedding_audit':{k:v for k,v in audit.items() if k!='cache_reencoding_details'},'dataset_sha256':plan['dataset_sha256'],'corpus_sha256':cfg['corpus_sha256'],
  'protected_files_unchanged':True,'qa_calls':0,'external_llm_calls':0,'network_connections_attempted':len(attempts),'production_changes':0,'seconds':time.time()-started,'completed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()}
 save(OUT/'results.json',{'cases':rows});save(OUT/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
