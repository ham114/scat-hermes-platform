import datetime,hashlib,json,sqlite3,sys,time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_holdout_resource_extension import configure
from ragas_holdout_budget_extension_v3 import ExtendedBudget as PriorBudget
from ragas_holdout_budget_extension_v4 import ExtendedBudget

def seed(folder,monkeypatch):
 p,digest=configure(folder);old=PriorBudget(folder)
 with sqlite3.connect(old.path) as db:
  rows=[(str(i),'old','ragas_generation',p['started_at']+1,'error_or_unknown',60792690 if i==0 else 0,13534024 if i==0 else 0,0,0,0,'hash') for i in range(4021)]
  db.executemany('insert into calls values(?,?,?,?,?,?,?,?,?,?,?)',rows)
 resumed=p['started_at']+14400
 a={'authorized':True,'user_reply':'授权上述总上限，补齐120道','original_policy_sha256':digest,'started_at':p['started_at'],'approved_at':datetime.datetime.fromtimestamp(resumed,datetime.timezone.utc).isoformat(),'expires_at':resumed+3600,'max_requests':5500,'max_provider_tokens':18000000,'max_conservative_units':100000000,'counters_reset':False,'start_reset':False}
 a['expires_at']=datetime.datetime.fromisoformat(a['approved_at']).timestamp()+3600
 (folder/'continuation-extension-approval.json').write_text(json.dumps(a))
 monkeypatch.setattr(time,'time',lambda:resumed+1)
 return old,p,a,digest

def test_expired_prior_authorization_continues_without_reclaiming_failures(tmp_path,monkeypatch):
 old,p,a,digest=seed(tmp_path,monkeypatch);before=old.snapshot();assert before['deadline_passed']
 b=ExtendedBudget(tmp_path)
 for key in ['requests','provider_budget_charge','conservative_units']:assert b.snapshot()[key]==before[key]
 assert b.policy['started_at']==p['started_at'] and not b.snapshot()['deadline_passed']
 r=b.admit('new','ragas_generation',[],2400);b.settle(r,error=RuntimeError())
 assert b.snapshot()['requests']==4022 and b.snapshot()['provider_budget_charge']>13534024
 assert hashlib.sha256((tmp_path/'policy.json').read_bytes()).hexdigest()==digest
 monkeypatch.setattr(time,'time',lambda:a['expires_at']+1)
 with pytest.raises(RuntimeError,match='budget_exhausted'):b.admit('late','ragas_generation',[],2400)
 assert b.snapshot()['requests']==4022

def test_changed_or_unapproved_extension_is_rejected(tmp_path,monkeypatch):
 seed(tmp_path,monkeypatch);path=tmp_path/'continuation-extension-approval.json'
 a=json.loads(path.read_text());a['max_requests']=5501;path.write_text(json.dumps(a))
 with pytest.raises(AssertionError):ExtendedBudget(tmp_path)
