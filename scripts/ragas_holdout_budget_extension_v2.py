"""Second explicit resource extension; immutable original policy and ledger."""
import hashlib,json,sqlite3,time
from pathlib import Path
from ragas_holdout_budget_extension import ExtendedBudget as PreviousBudget

def effective_policy(original,folder):
 folder=Path(folder);path=folder/'resource-extension-approval.json'
 approval=json.loads(path.read_text(encoding='utf8'))
 assert approval['authorized']is True
 assert approval['original_policy_sha256']==hashlib.sha256((folder/'policy.json').read_bytes()).hexdigest()
 assert approval['started_at']==original['started_at']
 assert approval['max_requests']==4500 and approval['max_provider_tokens']==14000000 and approval['max_conservative_units']==80000000
 assert approval['expires_at']==original['started_at']+10800==original['expires_at']+3600
 assert original['data_destination']=='https://api.deepseek.com'
 return {**original,**{k:approval[k]for k in ['max_requests','max_provider_tokens','max_conservative_units','expires_at']}}

class ExtendedBudget(PreviousBudget):
 def __init__(self,folder):
  super().__init__(folder);folder=Path(folder)
  original=json.loads((folder/'policy.json').read_text(encoding='utf8'))
  policy=effective_policy(original,folder)
  digest=hashlib.sha256((folder/'resource-extension-approval.json').read_bytes()).hexdigest()
  with sqlite3.connect(self.path,timeout=20)as db:
   db.execute('begin immediate');old=db.execute("select value from meta where key='resource_extension_approval_sha256'").fetchone()
   if old:assert old[0]==digest
   else:
    count,units,charge=db.execute('select count(*),coalesce(sum(reserved),0),coalesce(sum(charge),0)from calls').fetchone()
    assert count<=3000 and units<=40000000 and charge<=8000000 and time.time()<original['expires_at']
    assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
    db.execute("insert into meta values('resource_extension_approval_sha256',?)",(digest,))
  self.policy=policy
