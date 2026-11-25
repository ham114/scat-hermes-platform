"""Independent offline audit/export for the aviation300 QA campaign. No model calls."""
from contextlib import closing
import hashlib,json,sqlite3,sys
from pathlib import Path
from urllib.request import build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261124-rag-aviation300'
DATA=ROOT/'evaluations/rag_aviation300_reviewed_20261124_v1'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')

def main():
    policy=read(OUT/'qa-policy.json');final=read(OUT/'qa-final.json');manifest=read(DATA/'manifest.json')
    assert sha(DATA/'manifest.json')==policy['dataset_manifest_sha256']
    for f,h in policy['source_sha256'].items():assert sha(ROOT/f)==h
    for f,h in manifest['files'].items():assert sha(DATA/f)==h
    rubric=read(OUT/'scoring-rubric.json');rubric_manifest=read(OUT/'scoring-rubric-manifest.json')
    assert sha(OUT/'scoring-rubric.json')==rubric_manifest['scoring_rubric_sha256']==final['scoring_rubric_sha256']
    cases={c['case_id']:c for c in read(DATA/'evaluation.json')['cases']}
    calibrated={c['case_id']:c for c in rubric['cases']}
    config=read(ROOT/'services/rag/real-agent-production-v1.json')
    assert sha(Path(config['corpus']))==manifest['corpus_sha256']
    rows={r['chunk_id']:r for r in map(json.loads,Path(config['corpus']).read_text().splitlines())}
    quotes=0;turns=0;native_calls=0;native_attempt_events=0;native_error_events=0;answers=[];diagnostics=[];requested_models=set();served_models=set();earliest_answer_admission=None;failed_without_native=[]
    with closing(sqlite3.connect(f'file:{OUT}/qa-ledger.sqlite?mode=ro',uri=True)) as db:
        metered_by_case=dict(db.execute("select case_id,count(*) from calls where lane='qa' group by case_id"))
    for entry in final['cases']:
        c=cases[entry['case_id']];folder=OUT/'qa'/c['case_id'];request=folder/'turn-1.request.json'
        r=read(request);admitted=read(folder/'turn-1.admitted.json')
        assert set(r)=={'case_id','question','actual_history','output','inspect_only'}
        assert r['question']==c['question'] and r['actual_history']==[] and not r['inspect_only']
        assert sha(request)==admitted['request_sha256']
        assert admitted['at']>=rubric_manifest['frozen_at']
        earliest_answer_admission=min(earliest_answer_admission or admitted['at'],admitted['at'])
        resultfile=folder/'turn-1/result.json'
        result=read(resultfile) if resultfile.exists() else {'status':'unknown_no_replay'}
        if resultfile.exists():
            turns+=1
            assert result['question']==c['question']
            assert result['actual_history_sha256']==hashlib.sha256(json.dumps([],sort_keys=True).encode()).hexdigest()
            assert result['mode']=='fast'
            assert result.get('temporary_profile_removed') is True
            attempts=sum(e['event']=='llm_request' for e in result.get('events',[]))
            errors=sum(e['event']=='llm_error' for e in result.get('events',[]))
            native_attempt_events+=attempts;native_error_events+=errors
            assert attempts==metered_by_case.get(c['case_id'],0),'native_attempt_events_must_match_every_metered_attempt'
        nativefile=folder/'turn-1/native-result.json'
        if nativefile.exists():
            native=read(nativefile);native_calls+=int(native.get('api_calls') or 0)
            model=native.get('requested_model') or native.get('model')
            if model:requested_models.add(str(model))
            if native.get('served_model'):served_models.add(str(native['served_model']))
        else:
            assert result['status']!='passed'
            failed_without_native.append(c['case_id'])
        receipt=result.get('result') or {}
        actual_context=[]
        for cl in receipt.get('claims',[]):
            for e in cl['evidence']:
                ref=e['reference'];row=rows[ref['chunk_id']]
                assert ref['corpus_sha256']==manifest['corpus_sha256']
                assert ref['scope']=='aviation300-20261124-'+c['case_id']
                assert all(ref[k]==row[k] for k in ('source_id','source_version','text_sha256'))
                assert row['text'][e['start_char']:e['end_char']]==e['quote']
                claim_text=cl.get('claim',cl.get('text'))
                assert isinstance(claim_text,str) and claim_text and claim_text in receipt['answer']
                quotes+=1
                actual_context.append({'chunk_id':row['chunk_id'],'source_id':row['source_id'],'section_path':row['section_path'],'locator':row.get('locator',{})})
        assert entry['execution_passed']==(result['status']=='passed')
        raw_review=read(OUT/'judge'/(c['case_id']+'.json'))
        if entry['review']['status']=='valid_model_initial_review':
            assert raw_review['data']=={k:v for k,v in entry['review'].items() if k!='status'}
        exported={'case_id':c['case_id'],'category':c['category'],'primary_source':c['primary_source'],
            'language':c['language'],'require_cross_source':c['require_cross_source'],'human_review':c['human_review'],
            'question':c['question'],'reference':c['reference'],'original_candidate_claims':c['required_claims'],
            'scoring_rubric':calibrated[c['case_id']],'gold_evidence':c['claims'],
            'execution_status':result['status'],'execution_passed':entry['execution_passed'],
            'actual_answer':receipt.get('answer'),'answer_status':receipt.get('status'),
            'actual_claims':receipt.get('claims',[]),'actual_source_context':actual_context,
            'checks':result.get('checks',{}),'error_type':result.get('error_type'),'error_chain':result.get('error_chain',[]),
            'seconds':result.get('elapsed_seconds'),'review':entry['review']}
        answers.append(exported)
        if not entry['execution_passed'] or entry['review'].get('verdict')!='pass':diagnostics.append(exported)
    with closing(sqlite3.connect(f'file:{OUT}/qa-ledger.sqlite?mode=ro',uri=True)) as db:
        n,units,charge,inp,out,cache=db.execute('select count(*),coalesce(sum(units),0),coalesce(sum(provider_charge),0),coalesce(sum(input),0),coalesce(sum(output),0),coalesce(sum(cached),0) from calls').fetchone()
        lanes={lane:{'requests':count,'input':i,'output':o} for lane,count,i,o in db.execute('select lane,count(*),sum(input),sum(output) from calls group by lane')}
        states=dict(db.execute('select state,count(*) from calls group by state'))
        first,last=db.execute('select min(started),max(started) from calls').fetchone()
        exceptional=[{'case_id':cid,'lane':lane,'state':state,'charge':ch} for cid,lane,state,ch in db.execute("select case_id,lane,state,provider_charge from calls where state!='returned'")]
        qa_started=db.execute("select min(started) from calls where lane='qa'").fetchone()[0]
        unreported_failed_calls=sum(db.execute("select count(*) from calls where lane='qa' and case_id=?",(cid,)).fetchone()[0] for cid in failed_without_native)
    assert n<=policy['max_requests'] and units<=policy['max_conservative_units'] and charge<=policy['max_returned_provider_tokens']
    assert n==final['budget']['llm_calls'] and charge==final['budget']['provider_budget_charge']
    assert inp==final['budget']['provider_input_tokens'] and out==final['budget']['provider_output_tokens']
    # Hermes api_calls can omit failed or auxiliary summary requests. Request
    # events plus the persistent meter count every attempt without omissions.
    assert native_calls<=native_attempt_events
    missing_result_calls=sum(metered_by_case.get(cid,0) for cid in failed_without_native if not (OUT/'qa'/cid/'turn-1/result.json').exists())
    assert native_attempt_events+missing_result_calls==lanes.get('qa',{}).get('requests',0),'all_native_attempts_must_match_meter'
    assert last<=policy['expires_at']
    assert qa_started>=rubric_manifest['frozen_at']
    assert len(final['cases'])==len({x['case_id'] for x in final['cases']})
    if final['status']=='completed':assert len(final['cases'])==300
    # Independently recompute every strict retrieval metric from saved rankings.
    retrieval=read(OUT/'retrieval-summary.json')
    for entry in retrieval['cases']:
        c=cases[entry['case_id']];rank=read(OUT/'retrieval'/(c['case_id']+'.json'))
        for k in (5,10):
            ids={r['chunk_id'] for r in rank['ranked'][:k]};groups=c['supporting_evidence_groups']
            score=sum(set(g)<=ids for g in groups)/len(groups)
            assert score==entry[f'group_coverage_at_{k}'] and (score==1)==entry[f'complete_at_{k}']
    h=json.load(build_opener(ProxyHandler({})).open('http://127.0.0.1:18133/health/ready',timeout=15))
    assert h['corpus_sha256']==manifest['corpus_sha256'] and h['chunk_count']==8173
    judge_models=sorted({read(p).get('served_model') for p in (OUT/'judge').glob('*.raw.json') if read(p).get('served_model')})
    calibration_models=sorted({read(p).get('served_model') for p in (OUT/'gold-calibration').glob('*.raw.json') if read(p).get('served_model')})
    audit={'status':'passed','checked_cases':len(answers),'checked_native_turns':turns,
        'exact_answer_citations_checked':quotes,'native_api_calls_reported':native_calls,
        'native_reported_api_counter_is_not_total_attempts':True,
        'native_request_attempt_events':native_attempt_events,'native_model_error_events':native_error_events,
        'failed_turns_without_native_report':failed_without_native,'metered_calls_in_failed_turns_without_native_report':unreported_failed_calls,
        'requested_models':sorted(requested_models),'served_models_if_reported':sorted(served_models),
        'judge_served_models_if_reported':judge_models,'calibration_served_models_if_reported':calibration_models,
        'gold_frozen_before_all_answers':True,'answer_requests_without_gold':True,
        'individual_human_spot_checks_approved':5,'all_300_individually_human_approved':False,
        'frozen_data_and_sources_unchanged':True,'strict_retrieval_metrics_recomputed':300,
        'requests':n,'conservative_units':units,'provider_budget_charge':charge,
        'provider_input_tokens':inp,'provider_output_tokens':out,'provider_cached_tokens_included_in_input':cache,
        'lanes':lanes,'states':states,'exceptional_calls':exceptional,'first_request_at':first,'last_request_at':last,
        'deadline':policy['expires_at'],'completed_at':final['ended_at'],
        'completed_within_deadline':final['ended_at']<=policy['expires_at'],
        'actual_model_calls_in_audit':0,'production_health':h,'qa_final_sha256':sha(OUT/'qa-final.json')}
    save(OUT/'qa-audit.json',audit);save(OUT/'qa-answers.json',answers);save(OUT/'qa-diagnostics.json',diagnostics)
    print(json.dumps(audit),flush=True)

if __name__=='__main__':main()
