"""Serial CPU replay of saved reranker scores: zero new model inference."""
import datetime
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts')]
os.environ.update(OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',MKL_NUM_THREADS='4')
OUT=ROOT/'var/validation/20261125-rag-rerank-postprocess-v1'
BASE=ROOT/'output/rag-fusion-serial-20261125'
CORPUS=ROOT/'var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl'
VECTORS=CORPUS.parent/'bge-m3.npy'
DEPTHS=(5,10,20,50,100)


def read(p):return json.loads(p.read_text(encoding='utf8'))


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def save(p,v):
    p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix(p.suffix+'.writing');t.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8');t.replace(p)


def main():
    assert not (OUT/'summary.json').exists(),'completed replay is immutable'
    attempts=[]
    def deny(*a,**kw):attempts.append('blocked');raise RuntimeError('network_disabled_for_cached_rerank_replay')
    socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
    import numpy as np
    from rerank_postprocess_policies import POLICIES,rerank,SOURCE_ALIASES
    from evidence_retrieval_metrics import coverage
    raw=read(BASE/'weighted2-results.json')['cases']
    assert len(raw)==233 and all(r['scores'] and len(r['scores'])==100 for r in raw)
    old_plan=read(ROOT/'output/rag-evidence-top100-20261125/plan.json')
    assert sha(CORPUS)==old_plan['corpus_sha256'] and sha(VECTORS)==old_plan['embedding_cache_sha256']
    protected_paths=[BASE/'weighted2-results.json',BASE/'dataset.json',BASE/'summary.json',CORPUS,VECTORS,
                     ROOT/'src/rag_retrieval/baseline.py',ROOT/'services/rag/qwen3-production-v1.json',
                     ROOT/'services/rag/real-agent-production-v1.json',ROOT/'scripts/rerank_postprocess_policies.py',
                     ROOT/'scripts/run_rerank_postprocess_eval.py',ROOT/'scripts/evidence_retrieval_metrics.py']
    protected={p.relative_to(ROOT).as_posix():sha(p) for p in protected_paths}
    assert protected['src/rag_retrieval/baseline.py']==old_plan['source_sha256']['src/rag_retrieval/baseline.py']
    plan={'policies':POLICIES,'serial_order':[p['id'] for p in POLICIES],
          'source_aliases':SOURCE_ALIASES,'candidate_pool':'frozen weighted2 original-BM25100, same233 queries',
          'cohorts':{'new120':120,'old113':113},'protected_sha256':protected,
          'baseline_expected':{'new120':{'5':79,'10':97,'100':114},'old113':{'5':108,'10':111,'100':113}},
          'selection_rule':'Require zero old113 Top5/10 case regressions; prioritize new120 Top5 thenTop10; report all regressions and do not retune on this dataset.',
          'scorer_inputs':'query, candidate order, saved relevance scores, document metadata and saved BGE similarities only. No labels, case/cohort IDs or gold sources are passed into policies.',
          'qa_calls':0,'external_llm_calls':0,'new_model_inferences':0,'production_changes':0}
    if (OUT/'preregistration.json').exists():assert read(OUT/'preregistration.json')==plan
    else:save(OUT/'preregistration.json',plan)
    records={};ids=[]
    for row in map(json.loads,CORPUS.read_text(encoding='utf8').splitlines()):
        ids.append(row['chunk_id']);records[row['chunk_id']]={'source_id':row['source_id'],'section_path':row.get('section_path',[])}
    vectors=np.load(VECTORS,mmap_mode='r');assert vectors.shape==(len(ids),1024) and np.isfinite(vectors).all()
    assert np.allclose(np.linalg.norm(vectors,axis=1),1,atol=1e-3)
    index={cid:i for i,cid in enumerate(ids)};shared={};similarity_times=[]
    for r in raw:
        pool=r['pool'];scores=r['scores'];assert set(pool)==set(scores) and len(pool)==100
        positions={cid:i for i,cid in enumerate(pool)}
        baseline=sorted(pool,key=lambda cid:(-scores[cid],positions[cid]))
        assert baseline==r['ranking'],'saved baseline score/order mismatch'
        t=time.perf_counter();v=np.asarray(vectors[[index[c] for c in baseline]],dtype=np.float64)
        sim=np.clip(v@v.T,-1.,1.);assert np.isfinite(sim).all()
        similarity_times.append(time.perf_counter()-t);shared[r['id']]=sim
    started=time.time();summaries=[];baseline_results=None
    for policy in POLICIES:  # Serial replay: one complete policy before the next.
        cases=[]
        for number,r in enumerate(raw,1):
            t=time.perf_counter()
            ranked,info=rerank(policy,query=r['question'],pool=r['pool'],scores=r['scores'],
                               records=records,similarities=shared[r['id']])
            seconds=time.perf_counter()-t
            assert len(ranked)==len(set(ranked))==100 and set(ranked)==set(r['pool'])
            metrics=coverage(ranked,r['gold'],r['groups'])
            if policy['id']=='reranker':assert metrics==r['metrics']
            cases.append({'id':r['id'],'cohort':r['cohort'],'category':r['category'],'question':r['question'],
                          'gold':r['gold'],'ranking':ranked,'metrics':metrics,'seconds':seconds,'activation':info,
                          'gold_ranks':{cid:ranked.index(cid)+1 if cid in ranked else None for cid in r['gold']}})
            save(OUT/'progress.json',{'policy':policy['id'],'completed':number,'total':233,'new_model_inferences':0})
        if baseline_results is None:baseline_results={r['id']:r for r in cases}
        metrics={h:{str(k):{'cases':len(rs),'complete_cases':sum(r['metrics'][str(k)]['all_evidence'] for r in rs),
                           'evidence_hits':sum(r['metrics'][str(k)]['evidence_hits'] for r in rs),
                           'evidence_total':sum(len(r['gold']) for r in rs)} for k in DEPTHS}
                 for h in ['new120','old113'] for rs in [[r for r in cases if r['cohort']==h]]}
        paired={h:{str(k):{
            'gained':[r['id'] for r in cases if r['cohort']==h and r['metrics'][str(k)]['all_evidence'] and not baseline_results[r['id']]['metrics'][str(k)]['all_evidence']],
            'lost':[r['id'] for r in cases if r['cohort']==h and not r['metrics'][str(k)]['all_evidence'] and baseline_results[r['id']]['metrics'][str(k)]['all_evidence']]
        }for k in (5,10)}for h in ['new120','old113']}
        latency={h:{'p50_ms':float(np.percentile(values,50)*1000),'p95_ms':float(np.percentile(values,95)*1000)}
                 for h in ['new120','old113'] for values in [[r['seconds'] for r in cases if r['cohort']==h]]}
        summary={'policy':policy,'metrics':metrics,'paired_against_reranker':paired,'latency_cpu_postprocess':latency,
                 'explicit_source_rule_active_cases':sum(bool(r['activation']['reserved_source_candidates']) for r in cases),
                 'status':'completed','cases':233,'new_model_inferences':0}
        save(OUT/(policy['id']+'-results.json'),{'cases':cases});save(OUT/(policy['id']+'-summary.json'),summary)
        summaries.append(summary);print(json.dumps({'policy_completed':policy['id'],'metrics':metrics},ensure_ascii=False),flush=True)
    assert all(sha(ROOT/p)==h for p,h in protected.items()) and not attempts
    assert not any(n in sys.modules for n in ['torch','transformers','sentence_transformers','openai','ragas'])
    summary={'status':'completed','policies':summaries,'serial_order':[p['id'] for p in POLICIES],
             'cases_per_policy':233,'new_model_inferences':0,'qa_calls':0,'external_llm_calls':0,
             'network_connections_attempted':len(attempts),'production_changes':0,'protected_files_unchanged':True,
             'shared_similarity_p50_ms':float(np.percentile(similarity_times,50)*1000),
             'shared_similarity_p95_ms':float(np.percentile(similarity_times,95)*1000),
             'seconds_postprocessing_only':time.time()-started,
             'completed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()}
    save(OUT/'summary.json',summary)


if __name__=='__main__':main()
