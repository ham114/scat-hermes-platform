"""Explicit assistant adjudication of native samples incorrectly rejected by the judge."""
import fcntl,hashlib,json,sqlite3,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-ragas-holdout120'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,x):
 tmp=p.with_suffix(p.suffix+'.writing');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8');tmp.replace(p)
def main():
 decisions=read(OUT/'assistant-admission-adjudications.json')['items']
 with (OUT/'campaign.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  with sqlite3.connect(OUT/'calls.sqlite')as db:assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
  state=read(OUT/'state.json');plan=read(OUT/'plan.json');corpus=ROOT/'var/rag/corpora'/plan['corpus_generation']/'rag_chunks.v1.jsonl'
  assert hashlib.sha256(corpus.read_bytes()).hexdigest()==state['corpus_sha256']
  rows={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())};slots={r['slot']:r for r in read(OUT/'slots.json')}
  for d in decisions:
   if any(c['generation']['job']==d['job']for c in state['accepted']):continue
   assert not any(c['slot']==d['slot']for c in state['accepted'])
   job=d['job'];raw=read(OUT/'jobs'/f'{job}.ragas.json');review=read(OUT/'jobs'/f'{job}.review.json');scenario=read(OUT/'jobs'/f'{job}.scenario.json')
   for suffix,digest in d['native_files_sha256'].items():assert hashlib.sha256((OUT/'jobs'/f'{job}.{suffix}.json').read_bytes()).hexdigest()==digest
   assert review['verdict']=='reject'and d['kind']=='assistant source adjudication; not an independent human or model pass'
   assert all(s not in raw['user_input']for s in d.get('verified_absent_question_strings',[]))
   selected={n['properties']['document_metadata']['chunk_id']:n['properties']['page_content']for n in scenario['nodes']};assert len(selected)==2
   assert all(rows[cid]['text']==text for cid,text in selected.items())
   used={e['chunk_id']for c in state['accepted']for cl in c['claims']for e in cl['evidence']};assert not set(selected)&used
   claims=[]
   for claim in d['claims']:
    evidence=[]
    for e in claim['evidence']:
     r=rows[e['chunk_id']];quote=e['quote'];start=r['text'].find(quote)
     assert e['chunk_id']in selected and start>=0 and 8<=len(quote)<=600
     evidence.append({k:r[k]for k in ['chunk_id','source_id','source_version','text_sha256','section_path','locator']}|{'quote':quote,'start_char':start,'end_char':start+len(quote)})
    claims.append({'text':claim['text'],'evidence':evidence})
   assert {e['chunk_id']for c in claims for e in c['evidence']}==set(selected)
   assert {p['chunk_id']for p in d['context_removal_proof']}==set(selected)and all(p['missing_without_context']for p in d['context_removal_proof'])
   slot=slots[d['slot']]
   c={'schema_version':'aviation-ragas-candidate/1.0','qid':f"HO120-20261125-{slot['slot']:03d}",**slot,'question':raw['user_input'],'reference':raw['reference'],'claims':claims,'required_claims':[x['text']for x in claims],
      'supporting_evidence_groups':[list(x)for x in sorted({tuple(sorted(e['chunk_id']for e in cl['evidence']))for cl in claims})],
      'expected_status':'insufficient' if slot['category']=='insufficient' else 'answerable','difficulty':'multi-context source-scope distinction','corpus_sha256':state['corpus_sha256'],
      'generation':{'method':'ragas-0.4.3-native-synthesizer.generate_sample','synthesizer':'multi_hop_specific_query_synthesizer','job':job,'native_output':f'jobs/{job}.ragas.json','scenario':f'jobs/{job}.scenario.json'},
      'automatic_review':{'verdict':'reject','kind':'preserved provider rejection; superseded by explicit assistant source adjudication','reason':review.get('reason')},
      'construction_protocol_revision':d.get('construction_protocol_revision',8),'source_pair_plan':f'jobs/{job}.pair-plan.json','single_context_checks':[],
      'dependency_test':{'without_first_unanswerable':True,'without_second_unanswerable':True,'parallel_independent_questions':False,'bridge_relation':d['bridge_relation'],'kind':'assistant source proof; not model ablation'},
      'assistant_source_plan':{'file':d.get('source_plan_file','assistant-source-plans.json'),'slot':slot['slot'],'kind':'assistant source goal; actual question remains native Ragas'},
      'assistant_admission':{'file':'assistant-admission-adjudications.json','job':job},
      'assistant_source_review':{'decision':'retain','reason':d['reason'],'independent_human_review':False},'human_review':'not_performed','not_independent_human_admission':True}
   state['accepted'].append(c);state['used_chunks']=sorted(set(state['used_chunks'])|set(selected))
  state['updated_at']=time.time();save(OUT/'state.json',state)
  (OUT/'candidates.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n'for c in sorted(state['accepted'],key=lambda c:c['slot'])),encoding='utf8')
  print(json.dumps({'accepted':len(state['accepted']),'explicit_assistant_admissions':sum(bool(c.get('assistant_admission'))for c in state['accepted']),'new_model_calls':0,'counters_reset':False}))
if __name__=='__main__':main()
