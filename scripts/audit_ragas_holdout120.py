"""Native provenance, official-source, budget and local-only duplicate checks."""
import argparse
import collections
import hashlib
import json
import os
import re
import socket
import sqlite3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-ragas-holdout120'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                  OMP_NUM_THREADS='4',HF_HUB_DISABLE_TELEMETRY='1')
read=lambda p:json.loads(p.read_text(encoding='utf8'))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--partial',action='store_true');parser.add_argument('--similarity',action='store_true');a=parser.parse_args()
    # No generation, judgment or data export can occur in this audit process.
    def deny(*args,**kwargs):raise RuntimeError('audit_network_disabled')
    socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
    plan=read(OUT/'plan.json');policy=read(OUT/'policy.json');state=read(OUT/'state.json')
    assert sha(OUT/'plan.json')==policy['plan_sha256']
    cases=sorted(state['accepted'],key=lambda c:c['slot'])
    assert len(cases)==120 if not a.partial else 0<len(cases)<=120
    assert len({c['slot']for c in cases})==len(cases)
    corpus=ROOT/'var/rag/corpora'/plan['corpus_generation']/'rag_chunks.v1.jsonl'
    assert sha(corpus)==plan['corpus_sha256']==policy['corpus_sha256']
    records={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())}
    prior=read(OUT/'old-question-fingerprints.json');excluded=set(read(OUT/'prior-evidence-exclusions.json')['chunk_ids'])
    norm=lambda q:re.sub(r'[\W_]+','',q).casefold()
    assert len({norm(c['question'])for c in cases})==len(cases)
    assert not ({norm(c['question'])for c in cases}&{x['normalized']for x in prior})
    revisions=read(OUT/'assistant-label-revisions.json')if (OUT/'assistant-label-revisions.json').exists()else []
    revised={r['job']:r for r in revisions};quotes=0;cross=0;protocols=collections.Counter();flags=[];revised_count=0;used_ids=set();assistant_admissions=0
    adjudications={r['job']:r for r in read(OUT/'assistant-admission-adjudications.json')['items']} if (OUT/'assistant-admission-adjudications.json').exists() else {}
    for c in cases:
        assert c['qid']==f"HO120-20261125-{c['slot']:03d}"
        job=c['generation']['job'];scenario=read(OUT/'jobs'/(job+'.scenario.json'));raw=read(OUT/'jobs'/(job+'.ragas.json'));review=read(OUT/'jobs'/(job+'.review.json'))
        assert c['question']==raw['user_input']
        if job in revised:
            r=revised[job];assert r['qid']==c['qid'] and r['native_question_unchanged'] is True
            assert r['original_reference']==raw['reference'] and r['revised_reference']==c['reference'] and r['revised_claims']==c['claims']
            assert c['assistant_reference_revision']['question_unchanged'] is True;revised_count+=1
        else:assert c['reference']==raw['reference']
        assert c['generation']['method']=='ragas-0.4.3-native-synthesizer.generate_sample'
        selected={n['properties']['document_metadata']['chunk_id']:n['properties']['page_content']for n in scenario['nodes']}
        assert len(selected)==2 and not set(selected)&excluded
        assert not set(selected)&used_ids;used_ids.update(selected)
        if not a.partial:assert c['assistant_source_review']['decision']in ['retain','revise_reference']
        assert all(records[cid]['text']==text and records[cid]['source_id'] in plan['source_quotas']for cid,text in selected.items())
        assert [re.sub(r'^<\d+-hop>\n\n','',text)for text in raw['reference_contexts']]==list(selected.values())
        manual=c.get('assistant_admission')
        if manual:
            assert manual=={'file':'assistant-admission-adjudications.json','job':job}
            d=adjudications[job]
            assert d['kind']=='assistant source adjudication; not an independent human or model pass'
            assert review['verdict']==c['automatic_review']['verdict']=='reject'
            assert c['human_review']=='not_performed' and c['not_independent_human_admission'] is True
            assert c['single_context_checks']==[]
            assert c['dependency_test']['kind']=='assistant source proof; not model ablation'
            for suffix,digest in d['native_files_sha256'].items():assert sha(OUT/'jobs'/f'{job}.{suffix}.json')==digest
            assert all(s not in c['question'] for s in d.get('verified_absent_question_strings',[]))
            assert {p['chunk_id'] for p in d['context_removal_proof']}==set(selected)
            assert all(p['missing_without_context'] for p in d['context_removal_proof'])
            assert len(c['claims'])==len(d['claims'])
            for cl,dc in zip(c['claims'],d['claims']):
                assert cl['text']==dc['text']
                assert [(e['chunk_id'],e['quote']) for e in cl['evidence']]==[(e['chunk_id'],e['quote']) for e in dc['evidence']]
            assistant_admissions+=1
        else:
            assert review['verdict']=='pass' and review['multi_context_necessary'] is True
            assert review['leaks_required_answer_facts'] is False and review['optional_reference_claims_present'] is False
        assert len(c['claims'])in [1,2,3]
        evidence=set();sources=set()
        for claim in c['claims']:
            assert claim['text'] and claim['evidence']
            for e in claim['evidence']:
                row=records[e['chunk_id']];assert e['chunk_id']in selected
                for key in ['source_id','source_version','text_sha256','section_path','locator']:assert e[key]==row[key]
                assert row['text'][e['start_char']:e['end_char']]==e['quote'];quotes+=1;evidence.add(e['chunk_id']);sources.add(e['source_id'])
        assert evidence==set(selected)
        if not manual:assert evidence==set(review['needed_chunk_ids'])
        assert c['primary_source']in sources and len(sources)==(2 if c['require_cross_source'] else 1)
        cross+=len(sources)==2
        assert c['expected_status']==('insufficient' if c['category']=='insufficient' else 'answerable')
        for part in ([] if manual else [1,2]):
            v=read(OUT/'jobs'/(job+f'.ablation-{part}.json'))
            assert v['chunk_id']in selected and v['complete_answer_supported'] is False
            assert v['missing_question_requirements'] and v['supported_question_requirements']
            assert all(x['quote']in selected[v['chunk_id']] for x in v['supported_question_requirements'])
        revision=c['construction_protocol_revision'];assert revision in [2,3,4,5,6,7,8,9,10,11,12,13,14,15];protocols[revision]+=1
        if revision>=3:
            v=read(OUT/c['source_pair_plan']);assert v['viable'] is True and set(v['selected_chunk_ids'])==set(selected)
            admitted=read(OUT/'jobs'/(job+'.admitted.json'))
            assert 2<=len(admitted['planning_candidates'])<=6
            assert all(cid not in excluded and records[cid]['source_id']in plan['source_quotas']for cid in admitted['planning_candidates'])
            assert set(selected)<=set(admitted['planning_candidates'])
        if c.get('assistant_source_plan'):
            m=read(OUT/c['assistant_source_plan']['file'])['items'][str(c['slot'])]
            assert set(m['chunks'])==set(selected)
            assert all(f['quote']in selected[f['chunk_id']]for f in m['fact_quotes'])
        if len(c['question'])>450:flags.append({'qid':c['qid'],'kind':'long_question'})
        if c['category']=='numeric_conditions' and not re.search(r'多少|几|计算|换算|数值|上限|下限|范围|限值|how (many|much|long|far)|calculat|convert|numer|value|maximum|minimum|bound|time|speed|threshold|resolution',c['question'],re.I):flags.append({'qid':c['qid'],'kind':'numeric_intent_review'})
    categories=dict(collections.Counter(c['category']for c in cases));sources=dict(collections.Counter(c['primary_source']for c in cases));languages=dict(collections.Counter(c['language']for c in cases))
    if not a.partial:
        assert categories==plan['category_targets'] and sources==plan['source_quotas'] and languages==plan['languages'] and cross>=75
    with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)as db:
        calls,reserved,charge,inp,output=db.execute('select count(*),sum(reserved),sum(charge),sum(input),sum(output)from calls').fetchone()
        states=dict(db.execute('select state,count(*)from calls group by state'));lanes=dict(db.execute('select lane,count(*)from calls group by lane'))
        first,last=db.execute('select min(started),max(started)from calls').fetchone()
    effective_limit=policy['max_requests']
    if (OUT/'request-extension-approval.json').exists():
        approval=read(OUT/'request-extension-approval.json')
        assert approval['authorized'] is True and approval['max_requests']==3000
        assert approval['original_policy_sha256']==sha(OUT/'policy.json')
        for key in ['max_provider_tokens','max_conservative_units','expires_at']:assert approval[key]==policy[key]
        with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)as db:
            assert db.execute("select value from meta where key='request_extension_approval_sha256'").fetchone()[0]==sha(OUT/'request-extension-approval.json')
        effective_limit=approval['max_requests']
    original_deadline=policy['expires_at']
    if (OUT/'resource-extension-approval.json').exists():
        from ragas_holdout_budget_extension_v2 import effective_policy
        policy=effective_policy(policy,OUT);effective_limit=policy['max_requests']
        with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)as db:
            assert db.execute("select value from meta where key='resource_extension_approval_sha256'").fetchone()[0]==sha(OUT/'resource-extension-approval.json')
    previous_deadline=policy['expires_at']
    if (OUT/'continuation-extension-approval.json').exists():
        from ragas_holdout_budget_extension_v4 import effective_policy
        approval=read(OUT/'continuation-extension-approval.json')
        policy=effective_policy(read(OUT/'policy.json'),OUT);effective_limit=policy['max_requests']
        from datetime import datetime
        continuation_start=datetime.fromisoformat(approval['approved_at']).timestamp()
        with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)as db:
            assert db.execute("select value from meta where key='continuation_extension_approval_sha256'").fetchone()[0]==sha(OUT/'continuation-extension-approval.json')
            assert not db.execute('select count(*)from calls where started>? and started<?',(previous_deadline,continuation_start)).fetchone()[0], 'Calls during expired authorization gap'
    assert calls<=effective_limit and reserved<=policy['max_conservative_units'] and charge<=policy['max_provider_tokens']
    assert policy['started_at']<=first<=last<=policy['expires_at']
    if not a.partial:assert not states.get('admitted')
    assert set(lanes)<={'ragas_generation','fresh_context_review','single_context_sufficiency_review','source_pair_feasibility'}
    for p,h in plan['protected_files_sha256'].items():assert sha(ROOT/p)==h,p
    for name,h in policy['source_sha256'].items():assert sha(ROOT/'scripts'/name)==h
    for revision in [2,3,4,5,6,7,8,9,10,11,12,13,14,15]:
        if revision>=5 and not (OUT/f'generation-revision-{revision}.json').exists():continue
        v=read(OUT/f'generation-revision-{revision}.json');assert v['original_policy_sha256']==sha(OUT/'policy.json')
        assert v['driver_sha256']==sha(ROOT/'scripts'/f'ragas_holdout120_revision{revision}.py')
        assert v[f'core_v{revision}_sha256']==sha(ROOT/'scripts'/f'ragas_holdout120_core_v{revision}.py')
        if revision>=4:
            extension='ragas_holdout_budget_extension_v4.py'if revision>=10 else 'ragas_holdout_budget_extension_v3.py'if revision>=7 else 'ragas_holdout_budget_extension_v2.py'if revision==6 else 'ragas_holdout_budget_extension.py'
            assert v['budget_extension_sha256']==sha(ROOT/'scripts'/extension)
        if revision==8:assert v['manual_source_plans_sha256']==sha(OUT/'assistant-source-plans.json')
        if revision in [9,10,11]:assert v['manual_source_plans_sha256']==sha(OUT/'assistant-source-plans-v2.json')
        if revision==12:assert v['manual_source_plans_sha256']==sha(OUT/'assistant-source-plans-v3.json')
        if revision==13:assert v['manual_source_plans_sha256']==sha(OUT/'assistant-source-plans-v4.json')
        if revision==14:assert v['manual_source_plans_sha256']==sha(OUT/'assistant-source-plans-v5.json')
        if revision>=15:assert v['manual_source_plans_sha256']==sha(OUT/'assistant-source-plans-v6.json')
        if revision>=10:assert v['continuation_extension_approval_sha256']==sha(OUT/'continuation-extension-approval.json')
    result={'status':'partial_passed' if a.partial else 'passed','candidates':len(cases),'native_ragas_questions_verified':len(cases),
            'exact_quotes_checked':quotes,'cross_source':cross,'category_counts':categories,'primary_source_counts':sources,'corpus_sha256':sha(corpus),
            'assistant_revised_reference_count':revised_count,
            'explicit_assistant_admissions':assistant_admissions,'provider_review_and_two_ablation_passes':len(cases)-assistant_admissions,
            'language_counts':languages,'construction_protocol_counts':dict(protocols),'old_question_forms':len(prior),
            'historical_gold_chunk_ids_excluded':len(excluded),'requests':calls,'effective_max_requests':effective_limit,'conservative_units':reserved,
            'provider_budget_charge':charge,'provider_input_tokens':inp,'provider_output_tokens':output,
            'request_states':states,'request_lanes':lanes,'first_request_at':first,'last_request_at':last,
            'original_deadline':original_deadline,'effective_deadline':policy['expires_at'],'policy_sha256':sha(OUT/'policy.json'),
            'effective_limits':{k:policy[k]for k in ['max_requests','max_provider_tokens','max_conservative_units','expires_at']},
            'candidates_sha256':sha(OUT/'candidates.jsonl'),'semantic_quality_flags':flags,
            'qa_evaluation_calls':0,'production_changes':0,'audit_model_calls':0,'old_questions_left_server':False}
    save(OUT/('partial-audit.json' if a.partial else 'audit.json'),result)
    if a.similarity:
        import numpy as np
        from sentence_transformers import SentenceTransformer
        model=SentenceTransformer('/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181',device='cuda',local_files_only=True)
        vectors=model.encode([c['question']for c in cases]+[r['question']for r in prior],batch_size=8,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=False)
        n=len(cases);within=vectors[:n]@vectors[:n].T;previous=vectors[:n]@vectors[n:].T
        pairs=[{'left':cases[i]['qid'],'right':cases[j]['qid'],'cosine':float(within[i,j]),'left_question':cases[i]['question'],'right_question':cases[j]['question']}
               for i in range(n)for j in range(i+1,n)if float(within[i,j])>=.87]
        old=[]
        for i,c in enumerate(cases):
            j=int(previous[i].argmax());score=float(previous[i,j])
            if score>=.85:old.append({'qid':c['qid'],'cosine':score,'question':c['question'],'prior_question':prior[j]['question']})
        similarity={'method':'local pinned BGE-M3; semantic flags need assistant adjudication, not automatic duplicate proof',
                    'candidates':n,'previous_question_count':len(prior),'within_batch_threshold':.87,'previous_topic_threshold':.85,
                    'within_batch_pairs':pairs,'previous_topic_flags':old,'old_questions_left_server':False,'external_model_calls':0,
                    'candidates_sha256':result['candidates_sha256']}
        save(OUT/'similarity-audit.json',similarity);np.save(OUT/'question-vectors.npy',vectors[:n])
        result['similarity_flags']={'within_batch':len(pairs),'prior':len(old)}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
