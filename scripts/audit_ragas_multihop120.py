"""Offline provenance, quote, quota, budget and optional embedding duplicate audits."""
import collections,hashlib,json,os,re,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261124-ragas-multihop120'
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4')
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,o):p.write_text(json.dumps(o,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
 plan=read(OUT/'plan.json');policy=read(OUT/'policy.json');state=read(OUT/'state.json')
 effective_request_limit=policy['max_requests']
 extension=OUT/'request-extension-approval.json'
 if extension.exists():
  approval=read(extension);assert approval['authorized'] is True and approval['max_requests']==1200
  assert approval['original_policy_sha256']==sha(OUT/'policy.json')
  for k in ['max_provider_tokens','max_conservative_units','expires_at']:assert approval[k]==policy[k]
  effective_request_limit=approval['max_requests']
 cases=sorted(state['accepted'],key=lambda c:c['slot']);assert len(cases)==120
 assert {c['slot'] for c in cases}==set(range(1,121))
 assert dict(collections.Counter(c['category'] for c in cases))==plan['category_targets']
 assert dict(collections.Counter(c['primary_source'] for c in cases))==plan['source_quotas']
 assert dict(collections.Counter(c['language'] for c in cases))==plan['languages']
 corpus=ROOT/'var/rag/corpora'/plan['corpus_generation']/'rag_chunks.v1.jsonl'
 assert sha(corpus)==plan['corpus_sha256']==policy['corpus_sha256']
 rows={r['chunk_id']:r for r in map(json.loads,corpus.read_text().splitlines())}
 prior=read(OUT/'old-question-fingerprints.json');norm=lambda q:re.sub(r'[\W_]+','',q).casefold()
 label_revisions={r['qid']:r for r in read(OUT/'assistant-label-revisions.json')['revisions']} if (OUT/'assistant-label-revisions.json').exists() else {}
 assert len({norm(c['question']) for c in cases})==120
 assert not ({norm(c['question']) for c in cases}&{r['normalized'] for r in prior})
 quotes=0;cross=0;flags=[]
 for c in cases:
  job=c['generation']['job'];raw=read(OUT/'jobs'/(job+'.ragas.json'));scenario=read(OUT/'jobs'/(job+'.scenario.json'));review=read(OUT/'jobs'/(job+'.review.json'))
  assert c['question']==raw['user_input']
  if c['qid'] in label_revisions:
   edit=label_revisions[c['qid']];assert edit['native_job']==job and edit['original_reference']==raw['reference'] and edit['revised_reference']==c['reference'] and edit['revised_claims']==c['claims']
  else:assert c['reference']==raw['reference']
  assert c['generation']['method']=='ragas-0.4.3-native-synthesizer.generate_sample'
  assert review['verdict']=='pass' and c['human_review']=='not_performed'
  selected={n['properties']['document_metadata']['chunk_id']:n['properties']['page_content'] for n in scenario['nodes']}
  assert len(selected)==2 and all(rows[cid]['text']==text and rows[cid]['source_id'] in plan['source_quotas'] for cid,text in selected.items())
  assert [re.sub(r'^<\d+-hop>\n\n','',s) for s in raw['reference_contexts']]==list(selected.values())
  evidence=set();sources=set()
  for claim in c['claims']:
   for e in claim['evidence']:
    r=rows[e['chunk_id']];assert e['chunk_id'] in selected
    assert all(e[k]==r[k] for k in ['source_id','source_version','text_sha256','locator','section_path'])
    assert r['text'][e['start_char']:e['end_char']]==e['quote'];quotes+=1;evidence.add(e['chunk_id']);sources.add(e['source_id'])
  assert evidence==set(selected) and c['primary_source'] in sources
  assert review['multi_context_necessary'] is True and set(review['needed_chunk_ids'])==evidence
  d=c['dependency_test'];assert d['without_first_unanswerable'] and d['without_second_unanswerable'] and d['parallel_independent_questions'] is False
  assert len(sources)==(2 if c['require_cross_source'] else 1)
  cross+=len(sources)==2
  assert c['expected_status']==('insufficient' if c['category']=='insufficient' else 'answerable')
  if c['category']=='numeric_conditions' and not re.search(r'多少|几|计算|换算|数值|上限|下限|范围|多长|限值|how (many|much|long|far)|calculat|convert|numer|value|maximum|minimum|bound|altitude|time|speed|limit|threshold|resolution',c['question'],re.I):flags.append({'qid':c['qid'],'kind':'numeric_question_intent','question':c['question']})
  if len(c['question'])>450:flags.append({'qid':c['qid'],'kind':'long_question'})
 assert cross>=75
 with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True) as db:
  calls,units,charge,inp,output=db.execute('select count(*),sum(reserved),sum(charge),sum(input),sum(output) from calls').fetchone()
  states=dict(db.execute('select state,count(*) from calls group by state'));lanes=dict(db.execute('select lane,count(*) from calls group by lane'))
  first,last=db.execute('select min(started),max(started) from calls').fetchone()
 assert calls<=effective_request_limit and units<=policy['max_conservative_units'] and charge<=policy['max_provider_tokens']
 assert not states.get('admitted') and last<=policy['expires_at']
 for name,digest in policy['source_sha256'].items():assert sha(ROOT/'scripts'/name)==digest
 revision=read(OUT/'prompt-revision-2.json');assert revision['script_sha256']==sha(ROOT/'scripts/ragas_multihop120_revision.py')
 assert revision['original_policy_sha256']==sha(OUT/'policy.json')
 for number in (3,4,5,6,7):
  revision=read(OUT/f'prompt-revision-{number}.json');assert revision['script_sha256']==sha(ROOT/'scripts'/f'ragas_multihop120_revision{number}.py')
  assert revision['original_policy_sha256']==sha(OUT/'policy.json')
 if extension.exists():
  with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True) as db:assert db.execute("select value from meta where key='request_extension_approval_sha256'").fetchone()[0]==sha(extension)
 result={'status':'passed','candidates':120,'native_ragas_samples_verified':120,'exact_evidence_quotes_checked':quotes,'cross_source':cross,'primary_source_counts':dict(collections.Counter(c['primary_source'] for c in cases)),'category_counts':dict(collections.Counter(c['category'] for c in cases)),'language_counts':dict(collections.Counter(c['language'] for c in cases)),'requests':calls,'conservative_units':units,'provider_budget_charge':charge,'provider_input_tokens':inp,'provider_output_tokens':output,'request_states':states,'request_lanes':lanes,'first_request_at':first,'last_request_at':last,'original_deadline':policy['expires_at'],'candidates_sha256':sha(OUT/'candidates.jsonl'),'ragas_export_sha256':sha(OUT/'ragas-testset.json'),'corpus_sha256':plan['corpus_sha256'],'human_approved':0,'qa_system_calls':0,'semantic_review_flags':flags,'not_independent_human_admission':True,'external_model_calls_in_audit':0}
 result['effective_max_requests']=effective_request_limit;result['original_policy_unchanged']=True;result['assistant_revised_reference_count']=len(label_revisions)
 save(OUT/'audit.json',result)
 if 'similarity' in sys.argv:
  from sentence_transformers import SentenceTransformer
  import numpy as np
  model=SentenceTransformer('/mnt/h3c/scat-rag/models/embedding/bge-m3-5617a9f61b028005a4858fdac845db406aefb181',device='cuda',local_files_only=True)
  v=model.encode([c['question'] for c in cases]+[x['question'] for x in prior],batch_size=8,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=False)
  sims=v[:120]@v[:120].T;pairs=[]
  for i in range(120):
   for j in range(i+1,120):
    if float(sims[i,j])>=.87:pairs.append({'left':cases[i]['qid'],'right':cases[j]['qid'],'cosine':float(sims[i,j]),'left_question':cases[i]['question'],'right_question':cases[j]['question']})
  previous=v[:120]@v[120:].T;old_pairs=[]
  for i,c in enumerate(cases):
   j=int(previous[i].argmax());score=float(previous[i,j])
   if score>=.85:old_pairs.append({'qid':c['qid'],'cosine':score,'question':c['question'],'prior_question':prior[j]['question']})
  duplicate={'method':'local pinned BGE-M3 question similarity; flags require semantic adjudication','candidates':120,'previous_question_count':len(prior),'within_batch_threshold':.87,'within_batch_pairs':sorted(pairs,key=lambda x:-x['cosine']),'previous_topic_threshold':.85,'previous_topic_flags':sorted(old_pairs,key=lambda x:-x['cosine']),'external_model_calls':0,'old_questions_left_server':False,'candidates_sha256':sha(OUT/'candidates.jsonl')}
  save(OUT/'similarity-audit.json',duplicate);np.save(OUT/'question-vectors.npy',v[:120]);result['similarity_flags']={'within_batch':len(pairs),'previous':len(old_pairs)}
 print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
