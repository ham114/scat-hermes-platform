"""Offline Ragas export after construction review; never invoke the QA system."""
import collections, hashlib, importlib.metadata, json, os, socket, sqlite3, time
from pathlib import Path
os.environ.update(RAGAS_DO_NOT_TRACK='true',LANGCHAIN_TRACING_V2='false',LANGSMITH_TRACING='false',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-ragas-holdout120'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
 def deny(*a,**kw):raise RuntimeError('offline_export_network_disabled')
 socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
 import fcntl
 with (OUT/'campaign.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  state=read(OUT/'state.json');cases=sorted(state['accepted'],key=lambda c:c['slot']);plan=read(OUT/'plan.json')
  with sqlite3.connect(f'file:{OUT}/calls.sqlite?mode=ro',uri=True)as db:
   assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
   count,units,charge,inp,out=db.execute('select count(*),sum(reserved),sum(charge),sum(input),sum(output)from calls').fetchone()
  corpus=ROOT/'var/rag/corpora'/plan['corpus_generation']/'rag_chunks.v1.jsonl'
  assert hashlib.sha256(corpus.read_bytes()).hexdigest()==plan['corpus_sha256']
  byid={r['chunk_id']:r for r in map(json.loads,corpus.read_text(encoding='utf8').splitlines())}
  from ragas import SingleTurnSample
  from ragas.testset.synthesizers.testset_schema import Testset,TestsetSample
  native=Testset(samples=[TestsetSample(eval_sample=SingleTurnSample(user_input=c['question'],reference=c['reference'],reference_contexts=[byid[cid]['text']for cid in dict.fromkeys(e['chunk_id']for cl in c['claims']for e in cl['evidence'])]),synthesizer_name=c['generation']['synthesizer'])for c in cases])
  native.to_pandas().to_json(OUT/'ragas-testset.json',orient='records',force_ascii=False,indent=2)
  (OUT/'candidates.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n'for c in cases),encoding='utf8')
  export={'samples':len(cases),'ragas_version':importlib.metadata.version('ragas'),'export':'ragas-testset.json','external_qa_calls':0,'export_model_calls':0,'reference_revisions_explicit':True,'exported_at':time.time()}
  save(OUT/'final-native-export.json',export)
  state['status']='assistant-reviewed-needs-spot-review'if len(cases)==120 else 'assistant-reviewed-incomplete'
  if (OUT/'continuation-extension-approval.json').exists():
   from ragas_holdout_budget_extension_v4 import effective_policy
  else:
   from ragas_holdout_budget_extension_v2 import effective_policy
  policy=effective_policy(read(OUT/'policy.json'),OUT)
  state['budget']={**state.get('budget',{}),'requests':count,'conservative_units':units,'provider_budget_charge':charge,'known_input_tokens':inp,'known_output_tokens':out,'effective_deadline':policy['expires_at'],'deadline_passed':time.time()>policy['expires_at']}
  save(OUT/'state.json',state)
  summary={k:v for k,v in state.items()if k not in ['accepted','excluded','used_chunks','attempts']}
  summary.update(count=len(cases),target=120,by_source=dict(collections.Counter(c['primary_source']for c in cases)),by_category=dict(collections.Counter(c['category']for c in cases)),by_language=dict(collections.Counter(c['language']for c in cases)),excluded_count=len(state['excluded']),human_approved=0)
  save(OUT/'summary.json',summary);print(json.dumps(export))
if __name__=='__main__':main()
