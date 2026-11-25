"""Seal a fully audited question set; block further campaign admissions."""
import fcntl,hashlib,json,socket,sqlite3,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'var/validation/20261125-ragas-holdout120'
read=lambda p:json.loads(p.read_text(encoding='utf8'))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,x):
 t=p.with_suffix(p.suffix+'.writing');t.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8');t.replace(p)
def main():
 def deny(*a,**k):raise RuntimeError('offline_freeze_network_disabled')
 socket.socket.connect=socket.socket.connect_ex=socket.create_connection=deny
 with (OUT/'campaign.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  audit=read(OUT/'audit.json');sim=read(OUT/'similarity-audit.json');notes=read(OUT/'assistant-quality-notes.json')
  digest=sha(OUT/'candidates.jsonl')
  assert audit['status']=='passed' and audit['candidates']==120
  assert audit['candidates_sha256']==sim['candidates_sha256']==notes['candidates_sha256']==digest
  with sqlite3.connect(OUT/'calls.sqlite',timeout=20)as db:
   db.execute('begin immediate')
   assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
   count,units,charge=db.execute('select count(*),sum(reserved),sum(charge)from calls').fetchone()
   assert (count,units,charge)==(audit['requests'],audit['conservative_units'],audit['provider_budget_charge'])
   prior=db.execute("select value from meta where key='exhausted'").fetchone()
   db.execute("insert or replace into meta(key,value)values('exhausted','generation_complete')")
  record={'status':'frozen-awaiting-three-spot-checks','frozen_at':time.time(),'candidates':120,'candidates_sha256':digest,
   'audit_sha256':sha(OUT/'audit.json'),'similarity_audit_sha256':sha(OUT/'similarity-audit.json'),'quality_notes_sha256':sha(OUT/'assistant-quality-notes.json'),
   'requests':count,'conservative_units':units,'provider_budget_charge':charge,'previous_stop_reason':prior[0]if prior else None,
   'new_stop_reason':'generation_complete','counters_reset':False,'deadline_reset':False,'freeze_model_calls':0,'qa_evaluation_calls':0,'production_changes':0}
  if not (OUT/'generation-freeze.json').exists():save(OUT/'generation-freeze.json',record)
  state=read(OUT/'state.json');state['status']='frozen-awaiting-three-spot-checks';state['budget']['exhausted']='generation_complete'
  save(OUT/'state.json',state);summary=read(OUT/'summary.json');summary['status']=state['status'];summary['budget']=state['budget'];save(OUT/'summary.json',summary)
  print(json.dumps(read(OUT/'generation-freeze.json')))
if __name__=='__main__':main()
