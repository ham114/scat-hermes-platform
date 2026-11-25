"""Complete only the dense control after a historical-result schema mismatch."""
import hashlib
import json
import os
import socket
import sys
import time
import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
OUT=ROOT/'var/validation/20261125-rag-fusion-serial-v1'
BASE=ROOT/'output/rag-evidence-top100-20261125'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
                  TOKENIZERS_PARALLELISM='false',RAGAS_DO_NOT_TRACK='true',
                  LANGCHAIN_TRACING_V2='false',LANGSMITH_TRACING='false',OMP_NUM_THREADS='4')


def adapt_saved_dense(pool,fresh_dense,original_dense,saved):
    """candidate_pool is a description; ranking entries contain actual IDs."""
    assert pool==fresh_dense[:100]==original_dense[:100]
    ranked=[e['chunk_id'] for e in saved['ranking']]
    assert len(ranked)==len(set(ranked))==len(pool) and set(ranked)==set(pool)
    return ranked


def main():
    from run_fusion_serial_eval import read,save,sha,aggregate,DEPTHS
    import fcntl
    assert not (OUT/'summary.json').exists(),'completed run preserved'
    lock=(OUT/'run.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    attempts=[]
    def deny(*a,**kw):attempts.append('blocked');raise RuntimeError('network_disabled_for_dense_control')
    socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
    plan=read(OUT/'preregistration.json');identity=read(OUT/'run-identity.json')
    protected={**plan['protected_files'],**plan['input_files_sha256']}
    assert all(sha(ROOT/p)==h for p,h in protected.items())
    assert all(sha(ROOT/'scripts'/p)==h for p,h in identity['scripts_sha256'].items())
    from fusion_policy_experiments import POLICIES,select
    from rag_retrieval.production import Retriever,load_config
    from rag_retrieval.rerank_comparison import order_by_scores
    from evidence_retrieval_metrics import coverage
    import torch
    import numpy as np
    assert POLICIES==plan['policies']
    data=read(OUT/'dataset.json')['cases'];assert sha(OUT/'dataset.json')==identity['dataset_sha256']
    prior=[read(OUT/(p['id']+'-summary.json')) for p in POLICIES[:-1]]
    assert all(s['status']=='completed' and s['cases']==233 for s in prior)
    adapter_hash=sha(Path(__file__));started=time.time()
    save(OUT/'completion-record.json',{'reason':'Original driver treated candidate_pool description as an ID list and ranking objects as strings. Only the cached pure-dense control adapter is corrected. No fusion policy, label, corpus or scorer changes.',
         'adapter_sha256':adapter_hash,'completed_policies_preserved':[p['id'] for p in POLICIES[:-1]],
         'run_identity':identity,'qa_calls':0,'external_llm_calls':0})
    engine=Retriever(ROOT,load_config(ROOT,'services/rag/qwen3-production-v1.json'))
    assert sum(v<0 for v in engine.index.bm25._idf.values())==37
    original={r['case_id']:r for r in read(BASE/'results.json')['cases']}
    saved={r['case_id']:r for r in read(BASE/'dense-pool-ablation.json')['results']}
    assert all(r['input_results_sha256']==sha(BASE/'results.json') for r in saved.values())
    results=[];policy=POLICIES[-1];assert policy['id']=='dense100'
    for number,u in enumerate(data,1):
        path=OUT/'policies/dense100'/(u['id']+'.json')
        if path.exists():
            entry=read(path);assert entry['run_identity']==identity and entry['completion_adapter_sha256']==adapter_hash
        else:
            leaf=read(OUT/'leaves'/(u['id']+'.json'));assert leaf['run_identity']==identity
            pool=select(policy,leaf['dense'],leaf['lexical'],leaf['exact'],engine.index.chunks_by_id,u['question'],plan['profile'])
            assert len(pool)==100
            seconds=None;scores=None;info=None;origin='saved_same_ordered_pool_same_model_control'
            if u['cohort']=='new120':
                ranked=adapt_saved_dense(pool,leaf['dense'],[e['chunk_id'] for e in original[u['id']]['rankings']['dense']],saved[u['id']])
            else:
                torch.cuda.synchronize();t=time.perf_counter()
                scores,info=engine.reranker.score(u['question'].strip(),[engine.index.chunks_by_id[c].retrieval_text() for c in pool])
                torch.cuda.synchronize();seconds=time.perf_counter()-t
                assert not info['truncated_pairs'] and all(map(np.isfinite,scores))
                ranked=order_by_scores(pool,scores);origin='fresh_local_rerank'
            entry={**u,'policy':'dense100','run_identity':identity,'pool':pool,'ranking':ranked,'origin':origin,
                   'candidate_metrics':coverage(pool,u['gold'],u['groups']),
                   'metrics':coverage(ranked,u['gold'],u['groups']),
                   'gold_final_ranks':{cid:ranked.index(cid)+1 if cid in ranked else None for cid in u['gold']},
                   'gold_leaf_ranks':leaf['gold_leaf_ranks'],'seconds':seconds,
                   'scores':dict(zip(pool,scores,strict=True)) if scores is not None else None,
                   'rerank_info':info,'old_baseline_top10_parity':None,'completion_adapter_sha256':adapter_hash}
            save(path,entry)
        results.append(entry);save(OUT/'progress.json',{'stage':'rerank','policy':'dense100','cohort':u['cohort'],'completed':number,'total':233,'qa_calls':0,'external_llm_calls':0})
        if number%20==0:print(json.dumps({'policy':'dense100','completed':number}),flush=True)
    baseline={r['id']:r for r in read(OUT/'baseline-results.json')['cases']}
    paired={cohort:{str(k):{
        'gained':[r['id'] for r in results if r['cohort']==cohort and r['metrics'][str(k)]['all_evidence'] and not baseline[r['id']]['metrics'][str(k)]['all_evidence']],
        'lost':[r['id'] for r in results if r['cohort']==cohort and not r['metrics'][str(k)]['all_evidence'] and baseline[r['id']]['metrics'][str(k)]['all_evidence']]
    }for k in (5,10)}for cohort in ['new120','old113']}
    latency={cohort:{'fresh_observations':len(values),'p50_seconds':float(np.percentile(values,50)) if values else None,'p95_seconds':float(np.percentile(values,95)) if values else None}
             for cohort in ['new120','old113'] for values in [[r['seconds'] for r in results if r['cohort']==cohort and r['seconds'] is not None]]}
    final={'policy':policy,'status':'completed','cases':233,'metrics':aggregate(results,'metrics'),'candidate_metrics':aggregate(results,'candidate_metrics'),
           'paired_against_baseline':paired,'latency':latency,'fresh_local_reranks':113,'saved_control_ranks':120,
           'old_baseline_top10_parity':0,'qa_calls':0,'external_llm_calls':0,'completion_adapter_sha256':adapter_hash}
    assert all(sha(ROOT/p)==h for p,h in protected.items()) and not attempts
    assert sha(Path(__file__))==adapter_hash and all(sha(ROOT/'scripts'/p)==h for p,h in identity['scripts_sha256'].items())
    save(OUT/'dense100-results.json',{'cases':results});save(OUT/'dense100-summary.json',final)
    summary={'status':'completed','policies':prior+[final],'cases_per_policy':233,'serial_order':[p['id'] for p in POLICIES],
             'run_identity':identity,'original_bm25_sha256':sha(ROOT/'src/rag_retrieval/baseline.py'),'original_bm25_negative_idf_terms':37,
             'protected_files_unchanged':True,'production_changes':0,'qa_calls':0,'external_llm_calls':0,
             'network_connections_attempted':len(attempts),'seconds':time.time()-(OUT/'run-identity.json').stat().st_mtime,
             'completion_seconds':time.time()-started,'completion_adapter_sha256':adapter_hash,
             'completed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()}
    save(OUT/'summary.json',summary)
    print(json.dumps({'policy_completed':'dense100','metrics':final['metrics']},ensure_ascii=False),flush=True)
    print(json.dumps({'status':'completed'}),flush=True)


if __name__=='__main__':main()
