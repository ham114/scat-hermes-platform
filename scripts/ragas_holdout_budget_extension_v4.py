"""Explicit continuation after expiry; preserve every earlier ledger entry."""
import hashlib,json,sqlite3,time
from pathlib import Path
from ragas_holdout_budget_extension_v3 import ExtendedBudget as PreviousBudget
from ragas_holdout_budget_extension_v2 import effective_policy as prior_policy

def effective_policy(original,folder):
 folder=Path(folder);prior=prior_policy(original,folder)
 a=json.loads((folder/'continuation-extension-approval.json').read_text(encoding='utf8'))
 assert a['authorized'] is True and a['user_reply']=='授权上述总上限，补齐120道'
 assert a['original_policy_sha256']==hashlib.sha256((folder/'policy.json').read_bytes()).hexdigest()
 assert a['started_at']==original['started_at'] and a['counters_reset'] is False and a['start_reset'] is False
 assert a['max_requests']==5500 and a['max_provider_tokens']==18000000 and a['max_conservative_units']==100000000
 from datetime import datetime
 assert a['expires_at']==datetime.fromisoformat(a['approved_at']).timestamp()+3600
 assert a['expires_at']>prior['expires_at'] and original['data_destination']=='https://api.deepseek.com'
 return {**prior,**{k:a[k] for k in ['max_requests','max_provider_tokens','max_conservative_units','expires_at']}}

class ExtendedBudget(PreviousBudget):
 def __init__(self,folder):
  super().__init__(folder);folder=Path(folder)
  original=json.loads((folder/'policy.json').read_text(encoding='utf8'))
  policy=effective_policy(original,folder)
  digest=hashlib.sha256((folder/'continuation-extension-approval.json').read_bytes()).hexdigest()
  with sqlite3.connect(self.path,timeout=20) as db:
   db.execute('begin immediate');old=db.execute("select value from meta where key='continuation_extension_approval_sha256'").fetchone()
   if old:assert old[0]==digest
   else:
    count,units,charge=db.execute('select count(*),coalesce(sum(reserved),0),coalesce(sum(charge),0)from calls').fetchone()
    assert count==4021 and units==60792690 and charge==13534024
    assert not db.execute("select count(*)from calls where state='admitted'").fetchone()[0]
    assert time.time()<policy['expires_at']
    db.execute("insert into meta values('continuation_extension_approval_sha256',?)",(digest,))
  self.policy=policy
