"""Independently verify native Ragas provenance, quotes, quotas and campaign limits."""
from contextlib import closing
import collections,fcntl,hashlib,json,re,sqlite3,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
P=ROOT.parent/'scat-hermes-platform'
OUT=ROOT/'reports/batch-20261124-aviation300'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    lock=(OUT/'campaign.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    policy=read(OUT/'policy.json');plan=read(OUT/'plan.json');state=read(OUT/'state.json')
    assert len(state['accepted'])==300 and state['status']=='generated-needs-review'
    path=P/'var/rag/corpora/kg_20261123_aviation_v5/rag_chunks.v1.jsonl'
    assert sha(path)==policy['corpus_sha256']==plan['corpus_sha256']
    rows={r['chunk_id']:r for r in map(json.loads,path.read_text().splitlines())}
    candidates=[json.loads(line) for line in (OUT/'candidates.jsonl').read_text().splitlines()]
    assert len(candidates)==300 and {c['slot'] for c in candidates}==set(range(1,301))
    assert len({re.sub(r'[\W_]+','',c['question']).casefold() for c in candidates})==300
    assert dict(collections.Counter(c['primary_source'] for c in candidates))==plan['source_quotas']
    assert dict(collections.Counter(c['category'] for c in candidates))==plan['category_targets']
    assert dict(collections.Counter(c['language'] for c in candidates))==plan['language_targets']
    quotes=0;cross=0;flags=[]
    numeric_intent=r'多少|单位|范围|分辨率|频率|几|比例|百分|最大|最小|上限|下限|精度|间隔|周期|时效|高度|距离|速度|时长|持续|公式|计算|换算|阈值|限制|数值|长度|时速|format|unit|range|resolution|how (many|much|long|far|high)|frequency|limit|maximum|minimum|interval|rate|percent|convert|calculate|value|duration|degree|altitude|velocity|speed|dimension|precision|threshold'
    for c in candidates:
        job=c['generation']['job'];raw=read(OUT/'jobs'/(job+'.ragas.json'));scenario=read(OUT/'jobs'/(job+'.scenario.json'));review=read(OUT/'jobs'/(job+'.review.json'))
        assert c['question']==raw['user_input'] and c['reference']==raw['reference']
        assert c['generation']['method']=='ragas-0.4.3-native-synthesizer.generate_sample'
        assert review['verdict']=='pass' and c['human_review']=='not_performed'
        selected={n['properties']['document_metadata']['chunk_id']:n['properties']['page_content'] for n in scenario['nodes']}
        assert all(rows[cid]['text']==text and rows[cid]['source_id'] in plan['source_quotas'] for cid,text in selected.items())
        assert len(raw['reference_contexts'])==len(selected)
        assert [re.sub(r'^<\d+-hop>\n\n','',s) for s in raw['reference_contexts']]==list(selected.values())
        evidence_ids=set();sources=set()
        for claim in c['claims']:
            for e in claim['evidence']:
                r=rows[e['chunk_id']]
                assert e['chunk_id'] in selected and all(e[k]==r[k] for k in ('source_id','source_version','text_sha256'))
                assert r['text'][e['start_char']:e['end_char']]==e['quote']
                assert e['locator']==r['locator']
                quotes+=1;evidence_ids.add(e['chunk_id']);sources.add(e['source_id'])
        if c['category']=='multi_hop':
            assert len(evidence_ids)==2 and review['multi_context_necessary'] is True and set(review['needed_chunk_ids'])==evidence_ids
            if len(sources)==2:cross+=1
            if c['require_cross_source']:assert len(sources)==2
        assert c['primary_source'] in sources
        assert c['expected_status']==('insufficient' if c['category']=='insufficient' else 'answerable')
        if c['category']=='numeric_units' and not re.search(numeric_intent,c['question'],re.I):flags.append({'qid':c['qid'],'kind':'numeric_type_needs_semantic_check','question':c['question'],'reference':c['reference']})
        if len(c['question'])>450:flags.append({'qid':c['qid'],'kind':'long_question','question':c['question']})
    assert cross>=30
    with closing(sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)) as db:
        calls,units,charge,inp,out=db.execute('select count(*),sum(reserved),sum(charge),sum(input),sum(output) from calls').fetchone()
        states=dict(db.execute('select state,count(*) from calls group by state'));lanes=dict(db.execute('select lane,count(*) from calls group by lane'))
        first,last=db.execute('select min(started),max(started) from calls').fetchone()
    assert calls<=policy['max_requests'] and units<=policy['max_conservative_units'] and charge<=policy['max_provider_tokens']
    assert not states.get('admitted') and last<=policy['expires_at'] and (OUT/'candidates.jsonl').stat().st_mtime<=policy['expires_at']
    for name,digest in policy['source_sha256'].items():assert sha(ROOT/'scripts'/name)==digest
    result={'status':'passed','candidates':300,'native_ragas_samples_verified':300,'exact_evidence_quotes_checked':quotes,
        'native_synthesizers':dict(collections.Counter(c['generation']['synthesizer'] for c in candidates)),
        'primary_source_counts':dict(collections.Counter(c['primary_source'] for c in candidates)),
        'category_counts':dict(collections.Counter(c['category'] for c in candidates)),
        'language_counts':dict(collections.Counter(c['language'] for c in candidates)),
        'cross_source_multihop':cross,'requests':calls,'conservative_units':units,'provider_budget_charge':charge,
        'provider_input_tokens':inp,'provider_output_tokens':out,'request_states':states,'request_lanes':lanes,
        'first_request_at':first,'last_request_at':last,'original_deadline':policy['expires_at'],
        'candidates_sha256':sha(OUT/'candidates.jsonl'),'ragas_export_sha256':sha(OUT/'ragas-testset.json'),
        'corpus_sha256':policy['corpus_sha256'],'human_approved':0,'not_independent_human_admission':True,
        'external_model_calls_in_audit':0,'qa_system_calls':0,'semantic_review_flags':flags}
    (OUT/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
