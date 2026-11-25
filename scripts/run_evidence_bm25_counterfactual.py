"""Only fix per-document frequency counting in an offline candidate experiment."""
import collections,sys,time
from pathlib import Path
import run_evidence_top100 as base
ROOT=base.ROOT;OUT=base.OUT;read=base.read;save=base.save;sha=base.sha
def main():
 assert not (OUT/'bm25-df-counterfactual.json').exists(),'completed experiment preserved'
 connections=base.block_network()
 import torch
 from rag_retrieval.production import load_config
 from rag_retrieval.runtime import load_chunk_records,build_jieba_tokenizer,M8_RRF_PROFILE
 from rag_retrieval.baseline import BM25Index,reciprocal_rank_fusion
 from rag_retrieval.enhancements import apply_source_boost,infer_source_ids
 from rag_retrieval.rerank_comparison import Qwen3Reranker,order_by_scores
 from evidence_retrieval_metrics import coverage
 from evidence_bm25_counterfactual import correct_document_frequency
 started=time.time();plan=read(OUT/'plan.json');rows=read(OUT/'results.json')['cases'];units={u['case_id']:u for u in read(base.DATA)['units']};assert len(rows)==120
 cfg=load_config(ROOT,'services/rag/qwen3-production-v1.json');chunks,records=load_chunk_records(Path(cfg['corpus']));byid={c.chunk_id:c for c in chunks}
 index=BM25Index(chunks,tokenizer=build_jieba_tokenizer(),k1=M8_RRF_PROFILE['bm25_k1'],b=M8_RRF_PROFILE['bm25_b']);df_audit=correct_document_frequency(index)
 manifest=read(ROOT/cfg['model_manifest'])
 for f in manifest['files']:
  p=Path(manifest['destination'])/f['path'];assert p.stat().st_size==f['size'] and sha(p)==f['sha256']
 torch.set_num_threads(4);torch.set_num_interop_threads(1);reranker=Qwen3Reranker(manifest['destination'],batch_size=4,max_length=2048)
 input_hash=sha(OUT/'results.json');results=[]
 for number,r in enumerate(rows,1):
  p=OUT/'bm25-df-cases'/(r['case_id']+'.json')
  if p.exists():entry=read(p);assert entry['input_results_sha256']==input_hash
  else:
   query=' '.join(r['question'].split());lexical=[c for c,_ in index.rank(query,limit=200)];dense=[e['chunk_id'] for e in r['rankings']['dense'][:200]];exact=[e['chunk_id'] for e in r['rankings']['exact'][:200]]
   rankings=[lexical,dense];weights=[1.,1.]
   if exact:rankings.append(exact);weights.append(M8_RRF_PROFILE['exact_weight'])
   fused=reciprocal_rank_fusion(rankings,rank_constant=M8_RRF_PROFILE['rrf_k'],weights=weights,limit=200)
   boosted=apply_source_boost(fused,byid,infer_source_ids(query),M8_RRF_PROFILE['source_multiplier']);pool=[cid for cid,_ in boosted[:100]]
   scores,info=reranker.score(r['question'].strip(),[byid[cid].retrieval_text() for cid in pool]);assert not info['truncated_pairs'];reranked=order_by_scores(pool,scores);u=units[r['case_id']]
   entry={'case_id':r['case_id'],'input_results_sha256':input_hash,'ranks':{'corrected_bm25':lexical,'corrected_candidate100':pool,'corrected_reranked':reranked},
    'metrics':{stage:coverage(ids,u['gold_chunk_ids'],u['supporting_evidence_groups']) for stage,ids in [('corrected_bm25',lexical),('corrected_candidate100',pool),('corrected_reranked',reranked)]},'reranker_input_info':info}
   save(p,entry)
  results.append(entry);save(OUT/'bm25-df-progress.json',{'completed':number,'total':120,'qa_calls':0,'external_llm_calls':0})
  if number%10==0:print({'bm25_counterfactual_completed':number},flush=True)
 assert all(sha(ROOT/p)==h for p,h in plan['protected_files'].items()) and all(sha(ROOT/p)==h for p,h in plan['source_sha256'].items()) and not connections
 by_stage={stage:{str(k):{'complete_cases':sum(r['metrics'][stage][str(k)]['all_evidence'] for r in results),'evidence_hits':sum(r['metrics'][stage][str(k)]['evidence_hits'] for r in results)} for k in [5,10,20,50,100]} for stage in ['corrected_bm25','corrected_candidate100','corrected_reranked']}
 paired={str(k):{'gained_cases':[r['case_id'] for r,a in zip(rows,results) if not r['metrics']['reranked'][str(k)]['all_evidence'] and a['metrics']['corrected_reranked'][str(k)]['all_evidence']],
  'lost_cases':[r['case_id'] for r,a in zip(rows,results) if r['metrics']['reranked'][str(k)]['all_evidence'] and not a['metrics']['corrected_reranked'][str(k)]['all_evidence']]} for k in [5,10,20,50,100]}
 save(OUT/'bm25-df-counterfactual.json',{'status':'completed','cases':120,'protocol':'Only replace erroneous term-occurrence document-frequency count by unique-per-document count in an isolated instance. Same dense200, exact200, RRF/source weights, candidate100 and local Qwen reranker.','df_audit':df_audit,'by_stage':by_stage,'paired_against_production':paired,'results':results,'script_sha256':sha(Path(__file__)),'helper_sha256':sha(ROOT/'scripts/evidence_bm25_counterfactual.py'),'input_results_sha256':input_hash,'production_source_and_configs_unchanged':True,'previous_llm_ledgers_unchanged':True,'qa_calls':0,'external_llm_calls':0,'network_connections_attempted':len(connections),'seconds':time.time()-started})
 print({'status':'completed','by_stage':by_stage,'paired':paired},flush=True)
if __name__=='__main__':main()
