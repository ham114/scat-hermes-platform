"""Change only candidate pool to BGE100; same local reranker, no QA/network."""
import collections,hashlib,json,sys,time
from pathlib import Path
import run_evidence_top100 as base
ROOT=base.ROOT;OUT=base.OUT;read=base.read;save=base.save;sha=base.sha
def main():
 assert not (OUT/'dense-pool-ablation.json').exists(),'completed ablation preserved'
 network=base.block_network()
 import torch
 from rag_retrieval.production import load_config
 from rag_retrieval.runtime import load_chunk_records
 from rag_retrieval.rerank_comparison import Qwen3Reranker,order_by_scores
 from evidence_retrieval_metrics import coverage
 started=time.time();summary=read(OUT/'summary.json');assert summary['cases']==120 and summary['status']=='completed'
 cfg=load_config(ROOT,'services/rag/qwen3-production-v1.json');assert cfg['corpus_sha256']==summary['corpus_sha256']
 manifest=read(ROOT/cfg['model_manifest'])
 for f in manifest['files']:
  path=Path(manifest['destination'])/f['path'];assert path.stat().st_size==f['size'] and sha(path)==f['sha256']
 chunks,records=load_chunk_records(Path(cfg['corpus']));texts={c.chunk_id:c.retrieval_text() for c in chunks}
 torch.set_num_threads(4);torch.set_num_interop_threads(1)
 reranker=Qwen3Reranker(manifest['destination'],batch_size=4,max_length=2048)
 rows=read(OUT/'results.json')['cases'];units={u['case_id']:u for u in read(base.DATA)['units']};results=[]
 protected=read(OUT/'plan.json')['protected_files'];config_hash=sha(ROOT/'services/rag/qwen3-production-v1.json');input_hash=sha(OUT/'results.json')
 for i,r in enumerate(rows,1):
  p=OUT/'dense-pool-cases'/(r['case_id']+'.json')
  if p.exists():entry=read(p);assert entry['input_results_sha256']==input_hash
  else:
   ids=[e['chunk_id'] for e in r['rankings']['dense'][:100]];assert len(ids)==100
   scores,info=reranker.score(r['question'].strip(),[texts[cid] for cid in ids]);assert not info['truncated_pairs']
   ranked=order_by_scores(ids,scores);u=units[r['case_id']]
   entry={'case_id':r['case_id'],'input_results_sha256':input_hash,'candidate_pool':'frozen BGE100 from main diagnostic','metrics':coverage(ranked,u['gold_chunk_ids'],u['supporting_evidence_groups']),
    'ranking':[{'rank':j+1,'chunk_id':cid,'score':dict(zip(ids,scores))[cid]} for j,cid in enumerate(ranked)],'reranker_input_info':info}
   save(p,entry)
  results.append(entry);save(OUT/'dense-pool-progress.json',{'completed':i,'total':120,'qa_calls':0,'external_llm_calls':0})
  if i%10==0:print(json.dumps({'dense_pool_ablation_completed':i}),flush=True)
 assert config_hash==sha(ROOT/'services/rag/qwen3-production-v1.json') and input_hash==sha(OUT/'results.json')
 assert all(sha(ROOT/p)==h for p,h in protected.items()) and not network
 table={str(k):{'complete_cases':sum(r['metrics'][str(k)]['all_evidence'] for r in results),'evidence_hits':sum(r['metrics'][str(k)]['evidence_hits'] for r in results)} for k in [5,10,20,50,100]}
 paired={str(k):{'gained_cases':[r['case_id'] for r,a in zip(rows,results) if not r['metrics']['reranked'][str(k)]['all_evidence'] and a['metrics'][str(k)]['all_evidence']],
  'lost_cases':[r['case_id'] for r,a in zip(rows,results) if r['metrics']['reranked'][str(k)]['all_evidence'] and not a['metrics'][str(k)]['all_evidence']]} for k in [5,10,20,50,100]}
 result={'status':'completed','cases':120,'hypothesis':'Isolate candidate-pool choice while keeping query/corpus/Qwen weights/prompt/batch/max-length fixed. Local relevance logits only; no answer generation.',
  'by_depth':table,'paired_against_production':paired,'results':results,'input_results_sha256':input_hash,'script_sha256':sha(Path(__file__)),
  'production_config_unchanged':True,'previous_llm_ledgers_unchanged':True,'qa_calls':0,'external_llm_calls':0,'network_connections_attempted':len(network),'seconds':time.time()-started}
 save(OUT/'dense-pool-ablation.json',result);print(json.dumps({'status':'completed','by_depth':table,'paired':paired,'external_llm_calls':0},ensure_ascii=False))
if __name__=='__main__':main()
