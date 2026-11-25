"""Apply explicit assistant source-review decisions at a quiescent checkpoint only."""
import argparse, collections, copy, fcntl, hashlib, json, time, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/validation/20261125-ragas-holdout120'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,x):
 tmp=p.with_suffix(p.suffix+'.writing');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8');tmp.replace(p)
def main():
 p=argparse.ArgumentParser();p.add_argument('decisions');a=p.parse_args()
 decisions=read(Path(a.decisions))
 with (OUT/'campaign.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  with sqlite3.connect(OUT/'calls.sqlite',timeout=20) as db:
   assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
  state=read(OUT/'state.json');byjob={c['generation']['job']:c for c in state['accepted']}
  plan=read(OUT/'plan.json');corpus=ROOT/'var/rag/corpora'/plan['corpus_generation']/'rag_chunks.v1.jsonl'
  assert hashlib.sha256(corpus.read_bytes()).hexdigest()==state['corpus_sha256']
  sources={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())}
  history=read(OUT/'assistant-curation-history.json')if (OUT/'assistant-curation-history.json').exists()else []
  revisions=read(OUT/'assistant-label-revisions.json')if (OUT/'assistant-label-revisions.json').exists()else []
  prior_jobs={x['job']for x in history};removed=set()
  for d in decisions['items']:
   job=d['job']
   if job in prior_jobs:continue
   assert job in byjob,job
   c=byjob[job];assert c['qid']==d['qid']
   record={'job':job,'qid':c['qid'],'action':d['action'],'reason':d['reason'],'at':time.time()}
   if d['action']=='reject':
    record['candidate']=copy.deepcopy(c);removed.add(job)
    state['excluded'].append({'job':job,'slot':c['slot'],'category':c['category'],'source':c['primary_source'],'error_type':'AssistantSourceAudit','reason':d['reason']})
   elif d['action']=='revise_reference':
    original=copy.deepcopy(c)
    claims=[]
    for edit in d['claims']:
     cl=copy.deepcopy(c['claims'][edit['original_index']]);cl['text']=edit.get('text',cl['text'])
     for extra in edit.get('additional_quotes',[]):
      template=next(e for claim in original['claims']for e in claim['evidence']if e['chunk_id']==extra['chunk_id'])
      quote=extra['quote'];text=sources[extra['chunk_id']]['text'];start=text.find(quote)
      assert start>=0 and 8<=len(quote)<=600
      cl['evidence'].append({**template,'quote':quote,'start_char':start,'end_char':start+len(quote)})
     claims.append(cl)
    assert {e['chunk_id']for cl in claims for e in cl['evidence']}=={e['chunk_id']for cl in original['claims']for e in cl['evidence']},'Reference repair cannot drop a required context'
    c['reference']=d['reference'];c['claims']=claims;c['required_claims']=[cl['text']for cl in claims]
    c['supporting_evidence_groups']=[list(x)for x in sorted({tuple(sorted(e['chunk_id']for e in cl['evidence']))for cl in claims})]
    c['assistant_reference_revision']={'reason':d['reason'],'question_unchanged':True,'independent_human_review':False,'record':'assistant-label-revisions.json'}
    revisions.append({'job':job,'qid':c['qid'],'reason':d['reason'],'original_reference':original['reference'],'original_claims':original['claims'],'revised_reference':c['reference'],'revised_claims':claims,'native_question_unchanged':True})
   else:assert d['action']=='retain'
   if job not in removed:c['assistant_source_review']={'decision':d['action'],'reason':d['reason'],'independent_human_review':False}
   history.append(record)
  state['accepted']=[c for c in state['accepted']if c['generation']['job']not in removed]
  for c in state['accepted']:
   assert len({e['chunk_id']for cl in c['claims']for e in cl['evidence']})==2
  # Release failed selection reservations, never accepted or explicitly quarantined evidence.
  quarantine=set(decisions.get('quarantine_chunk_ids',[]))
  risk=read(OUT/'source-risk-register.json')if (OUT/'source-risk-register.json').exists()else {'items':[]}
  for item in decisions.get('source_risks',[]):
   if item not in risk['items']:risk['items'].append(item)
  quarantine.update(cid for item in risk['items']for cid in item.get('chunk_ids',[]))
  keep=quarantine|{e['chunk_id']for c in state['accepted']for cl in c['claims']for e in cl['evidence']}
  released=len(set(state['used_chunks'])-keep);state['used_chunks']=sorted(keep)
  state['status']='assistant-reviewed-checkpoint';state['updated_at']=time.time()
  save(OUT/'assistant-curation-history.json',history);save(OUT/'assistant-label-revisions.json',revisions);save(OUT/'source-risk-register.json',risk);save(OUT/'state.json',state)
  (OUT/'candidates.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n'for c in sorted(state['accepted'],key=lambda c:c['slot'])),encoding='utf8')
  save(OUT/'latest-curation-checkpoint.json',{'accepted':len(state['accepted']),'removed':len(removed),'released_failed_chunk_reservations':released,'counters_reset':False,'deadline_reset':False,'decisions_sha256':hashlib.sha256(Path(a.decisions).read_bytes()).hexdigest()})
  print(json.dumps(read(OUT/'latest-curation-checkpoint.json')))
if __name__=='__main__':main()
