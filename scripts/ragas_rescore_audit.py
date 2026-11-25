"""Zero-model audit: frozen inputs, native formulas, actual contexts and usage."""
from collections import Counter,defaultdict
from contextlib import closing
import hashlib,json,math,sqlite3,statistics
import numpy as np
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT.parent/'ragas-evaluation/reports/standard-reeval-20261124'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def number(v):
    if isinstance(v,str) and v.lower() in ('false','true'):return int(v.lower()=='true')
    return int(v) if isinstance(v,(str,int,float,bool)) else None

def recalculate(name,raw,row,embedding_db):
    warnings=[];details={}
    if name=='faithfulness':
        assert len(raw)==2
        statements=raw[0]['statements'];verdicts=raw[1]['statements']
        if len(statements)!=len(verdicts):warnings.append('decomposed_statement_count_differs_from_verdict_count')
        flags=[number(v['verdict']) for v in verdicts]
        details={'statement_count':len(statements),'verdict_count':len(verdicts),'unsupported_statements':[v for v in verdicts if not number(v['verdict'])]}
        return sum(flags)/len(flags),details,warnings
    if name=='context_precision':
        assert len(raw)==len(row['initial_top5'])==5
        flags=[number(d['verdict']) for d in raw]
        numerator=sum(sum(flags[:i+1])/(i+1)*flag for i,flag in enumerate(flags))
        return numerator/(sum(flags)+1e-10),{'ranked_relevance_verdicts':flags},warnings
    if name=='context_recall':
        assert len(raw)==1
        claims=raw[0]['classifications'];assert claims
        flags=[number(d['attributed']) for d in claims]
        return sum(flags)/len(flags),{'reference_statements':len(claims),'uncovered_reference_statements':[c for c in claims if not number(c['attributed'])]},warnings
    if name=='answer_relevancy':
        assert len(raw)==3
        questions=[d['question'] for d in raw if d['question']]
        flags=[bool(number(d['noncommittal'])) for d in raw if d['question']]
        if not questions:return 0.,{'generated_questions':[]},warnings
        def vector(text):
            r=embedding_db.execute('select vector from vectors where digest=?',(hashlib.sha256(text.encode()).hexdigest(),)).fetchone()
            assert r,'missing_actual_cached_embedding';return json.loads(r[0])
        q=vector(row['user_input']);qn=math.sqrt(sum(x*x for x in q));sims=[]
        for text in questions:
            v=vector(text);norm=math.sqrt(sum(x*x for x in v))*qn
            sims.append(sum(x*y for x,y in zip(q,v))/norm)
        value=statistics.mean(sims)*(0 if all(flags) else 1)
        return value,{'generated_questions':questions,'noncommittal_flags':flags,'cosine_similarities':sims},warnings
    assert name=='factual_correctness'
    arrays=[];i=0
    for direction in range(2):
        claims=raw[i]['claims'];i+=1
        if not claims:arrays.append([]);continue
        verdicts=raw[i]['statements'];i+=1
        if len(claims)!=len(verdicts):warnings.append(f'direction_{direction}_claim_count_differs_from_verdict_count')
        arrays.append([bool(number(v['verdict'])) for v in verdicts])
        details['response_claims' if direction==0 else 'reference_claims']=[{'claim':v['statement'],'supported':bool(number(v['verdict'])),'reason':v.get('reason')} for v in verdicts]
    assert i==len(raw)
    tp=sum(arrays[0]);fp=len(arrays[0])-tp;fn=len(arrays[1])-sum(arrays[1])
    precision=tp/(tp+fp) if tp+fp else 0.;recall=tp/(tp+fn) if tp+fn else 0.
    f1=2*(precision*recall)/(precision+recall) if precision+recall else 0.
    details.update(tp=tp,fp=fp,fn=fn,precision_component=precision,recall_component=recall,
        component_note='Offline decomposition of the same native FactualCorrectness F1 judgments, not separately rerun metrics')
    return float(np.round(f1,2)),details,warnings

def aggregate(items):
    values=[r['value'] for r in items if r['status']=='scored']
    main=[r['value'] for r in items if r['status']=='scored' and r['primary_eligible']]
    def stats(v):return {'n':len(v),'mean':statistics.mean(v) if v else None,'median':statistics.median(v) if v else None,'min':min(v) if v else None,'max':max(v) if v else None}
    return {'jobs':len(items),'statuses':dict(Counter(r['status'] for r in items)),
        'all_scored_diagnostic':stats(values),'known_issue_isolated':stats(main),
        'not_primary_eligible':sum(not r['primary_eligible'] for r in items)}

def main():
    final=read(OUT/'final.json');policy=read(OUT/'policy.json');manifest=read(OUT/'input-manifest.json')
    assert sha(OUT/'inputs.jsonl')==manifest['inputs_sha256']==policy['inputs_sha256']
    assert sha(OUT/'input-manifest.json')==policy['input_manifest_sha256'] and sha(OUT/'jobs.json')==policy['jobs_sha256']
    for f,h in policy['source_sha256'].items():assert sha(ROOT/f)==h
    for f,h in policy['installed_metric_sources'].items():assert sha(Path(f))==h
    for f,h in manifest['source_files'].items():assert sha(Path(f))==h
    approval=read(OUT/'approval.json');assert approval['approved'] and approval['policy_draft_sha256']==sha(OUT/'policy-draft.json')
    assert approval['input_manifest_sha256']==sha(OUT/'input-manifest.json')
    draft=read(OUT/'policy-draft.json')
    assert {k:v for k,v in draft.items() if k not in ('started_at','expires_at')}=={k:v for k,v in policy.items() if k not in ('started_at','expires_at')}
    sources={}
    for epoch,name in [('v4','kg_20261026_research_v4'),('v5','kg_20261123_aviation_v5')]:
        path=ROOT/'var/rag/corpora'/name/'rag_chunks.v1.jsonl';assert sha(path)==manifest['corpus_shas'][epoch]
        sources[epoch]={r['chunk_id']:r for r in map(json.loads,path.read_text().splitlines())}
    rows={r['unit_id']:r for r in map(json.loads,(OUT/'inputs.jsonl').read_text().splitlines())}
    assert len(rows)==439 and len({r['case_id'] for r in rows.values()})==426
    contexts=0;history=0
    for r in rows.values():
        records=sources[r['corpus_epoch']]
        assert len(r['initial_top5'])==5
        for e in r['initial_top5']:
            original=records[e['chunk_id']];assert e['text']==original['text'] and e['text_sha256']==original['text_sha256'];contexts+=1
        for e in r['contexts_seen']:
            original=records[e['chunk_id']]
            assert e['text']==(original['text'] if e['visibility']=='full' else original['text'][:240]);contexts+=1
        folder=ROOT/'var/validation'/('20261124-rag-aviation300' if r['corpus_epoch']=='v5' else '20261123-rag-p0')/'qa'/r['case_id']
        request=read(folder/f"turn-{r['turn']}.request.json");actual=read(folder/f"turn-{r['turn']}/result.json")
        assert request['question']==r['question_raw'] and request['actual_history']==r['history']
        assert r['response']==(actual.get('result') or {}).get('answer')
        assert actual['status']==r['execution_status']
        if r['history']:history+=1
    assert history==13
    with closing(sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)) as db:
        calls=list(db.execute('select id,job,lane,started,state,reserved,charge,input,output,cached,prompt_sha256 from calls order by started'))
        identity=db.execute("select value from meta where key='policy_sha256'").fetchone()[0]
        assert identity==hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()
    grouped=defaultdict(list)
    for c in calls:
        grouped[c[1]].append(c);assert policy['started_at']<=c[3]<=policy['expires_at']
        assert c[2]==c[1].split('--')[-1]
        p=OUT/'calls'/(c[0]+'.json')
        if c[4]=='returned':
            response=read(p);usage=response['usage']
            assert usage['prompt_tokens']==c[7] and usage['completion_tokens']==c[8]
            assert c[6]==c[7]+c[8]
    jobs={j['job_id']:j for j in read(OUT/'jobs.json')};assert len(jobs)==2195
    assert set(grouped)<=set(jobs)
    assert len(final['results'])==len({r['job_id'] for r in final['results']})==2195
    recomputed=0;details={};warnings=[];bad_raw=[]
    with closing(sqlite3.connect(f'file:{OUT}/embeddings.sqlite?mode=ro',uri=True)) as embeddings:
        for r in final['results']:
            job=jobs[r['job_id']];assert all(r[k]==v for k,v in job.items())
            assert read(OUT/'results'/(r['job_id']+'.json'))==r
            events=grouped[r['job_id']]
            limit={'faithfulness':2,'answer_relevancy':3,'context_precision':5,'context_recall':1,'factual_correctness':4}[r['metric']]
            assert len(events)<=limit,'unexpected_retry_or_replay'
            if not r['applicable']:assert r['status']=='not_applicable_no_delivered_answer' and not events
            if r['status']=='scored':
                assert math.isfinite(r['value'])
                raw=[]
                for e in events:
                    p=OUT/'calls'/(e[0]+'.json');d=read(p)
                    assert d['job']==r['job_id'] and d['finish_reason']=='stop'
                    raw.append(json.loads(d['text'],strict=False))
                calculated,detail,flags=recalculate(r['metric'],raw,rows[r['unit_id']],embeddings)
                assert abs(calculated-r['value'])<1e-8,(r['job_id'],calculated,r['value'])
                recomputed+=1;details[r['job_id']]=detail
                if flags:warnings.append({'job_id':r['job_id'],'flags':flags})
            elif r['status']=='not_started_budget_or_deadline':assert not events
            elif r['applicable']:
                records=[]
                for e in events:
                    p=OUT/'calls'/(e[0]+'.json');response=read(p) if p.exists() else {}
                    records.append({'state':e[4],'finish_reason':response.get('finish_reason'),
                        'file':p.relative_to(OUT).as_posix() if p.exists() else None})
                details[r['job_id']]={'status':r['status'],'error_type':r.get('error_type'),'requests':records,
                    'note':'No score imputed and no automatic replay. Raw model responses retained where returned.'}
    requests=len(calls);units=sum(c[5] for c in calls);charge=sum(c[6] for c in calls);inp=sum(c[7] for c in calls);out=sum(c[8] for c in calls);cache=sum(c[9] for c in calls)
    assert requests<=policy['max_requests'] and units<=policy['max_conservative_units'] and charge<=policy['max_provider_tokens']
    assert requests==final['budget']['requests'] and charge==final['budget']['provider_budget_charge']
    assert inp==final['budget']['provider_input_tokens'] and out==final['budget']['provider_output_tokens']
    assert not any(c[4]=='admitted' for c in calls),'pending_requests_must_be_preserved_not_forgotten'
    old_qa={}
    for name,expected in [('20261123-rag-p0',702),('20261124-rag-aviation300',1906)]:
        with closing(sqlite3.connect(f'file:{ROOT}/var/validation/{name}/qa-ledger.sqlite?mode=ro',uri=True)) as db:count=db.execute('select count(*) from calls').fetchone()[0]
        assert count==expected;old_qa[name]=count
    groups={}
    for group in sorted({r['cohort']+'/'+('dialogue_turn' if r['category']=='dialogue_turn' else 'single_turn') for r in rows.values()}):
        selected={uid for uid,r in rows.items() if r['cohort']+'/'+('dialogue_turn' if r['category']=='dialogue_turn' else 'single_turn')==group}
        groups[group]={'units':len(selected),'cases':len({rows[uid]['case_id'] for uid in selected}),
            'metrics':{m:aggregate([r for r in final['results'] if r['unit_id'] in selected and r['metric']==m]) for m in ['faithfulness','answer_relevancy','context_precision','context_recall','factual_correctness']}}
    audit={'status':'passed','cases':426,'turns':439,'contexts_verified':contexts,'actual_history_prefixes_verified':13,
        'metric_jobs':2195,'metric_statuses':dict(Counter(r['status'] for r in final['results'])),
        'native_metric_formulas_independently_recomputed':recomputed,'claim_alignment_warnings':warnings,
        'source_and_input_hashes_unchanged':True,'qa_calls_this_campaign':0,'previous_qa_ledger_counts_unchanged':old_qa,
        'requests':requests,'conservative_units':units,'provider_budget_charge':charge,'provider_input_tokens':inp,
        'provider_output_tokens':out,'provider_cached_tokens_included_in_input':cache,
        'request_states':dict(Counter(c[4] for c in calls)),'lanes':dict(Counter(c[2] for c in calls)),
        'first_request_at':min(c[3] for c in calls),'last_request_at':max(c[3] for c in calls),'original_deadline':policy['expires_at'],
        'completed_at':final['completed_at'],'completed_within_deadline':final['completed_at']<=policy['expires_at'],
        'no_automatic_request_replay':True,'ragas_version':'0.4.3','native_metric_formulas_unchanged':True,
        'external_calls_in_audit':0,'final_sha256':sha(OUT/'final.json')}
    save(OUT/'audit.json',audit);save(OUT/'metric-details.json',details)
    save(OUT/'summary.json',{'status':final['status'],'groups':groups,'audit':audit,'known_reference_issues':manifest['reference_issues_quarantined'],
        'interpretation':'Continuous Ragas metric values, not pass rate or real-world accuracy. Reference facts, embedding choice and judge affect scores. v4/v5 and dialogues are separate groups; full diagnostic results and predetermined issue-isolated means both retained.'})
    print(json.dumps(audit),flush=True)
if __name__=='__main__':main()
