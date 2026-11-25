import hashlib,json,sys,time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from ragas_aviation_budget import CampaignBudget
from ragas_holdout_budget_extension_v2 import ExtendedBudget

def configure(folder):
 now=time.time();p={'started_at':now,'expires_at':now+7200,'max_requests':1200,'max_provider_tokens':8000000,'max_conservative_units':40000000,'data_destination':'https://api.deepseek.com'}
 path=folder/'policy.json';path.write_text(json.dumps(p));digest=hashlib.sha256(path.read_bytes()).hexdigest()
 a={'authorized':True,'max_requests':3000,'original_policy_sha256':digest,**{k:p[k]for k in ['expires_at','max_provider_tokens','max_conservative_units']}}
 (folder/'request-extension-approval.json').write_text(json.dumps(a))
 a.update(started_at=now,expires_at=now+10800,max_requests=4500,max_provider_tokens=14000000,max_conservative_units=80000000)
 (folder/'resource-extension-approval.json').write_text(json.dumps(a));return p,digest

def test_extension_keeps_consumption_policy_and_start(tmp_path):
 p,digest=configure(tmp_path);old=CampaignBudget(tmp_path);r=old.admit('existing','generation',[],2400);old.settle(r,error=RuntimeError())
 before=old.snapshot();b=ExtendedBudget(tmp_path)
 for key in ['requests','provider_budget_charge','conservative_units']:assert b.snapshot()[key]==before[key]
 assert b.policy['expires_at']==p['started_at']+10800 and b.policy['started_at']==p['started_at']
 assert b.policy['max_requests']==4500 and b.policy['max_provider_tokens']==14000000 and b.policy['max_conservative_units']==80000000
 assert hashlib.sha256((tmp_path/'policy.json').read_bytes()).hexdigest()==digest

def test_authorized_resume_after_original_deadline(tmp_path,monkeypatch):
 p,_=configure(tmp_path);b=ExtendedBudget(tmp_path)
 monkeypatch.setattr(time,'time',lambda:p['expires_at']+1)
 resumed=ExtendedBudget(tmp_path);r=resumed.admit('new','review',[],2400)
 assert resumed.snapshot()['requests']==1 and not resumed.snapshot()['deadline_passed']
 resumed.settle(r,error=RuntimeError())

def test_cannot_expand_beyond_explicit_approval(tmp_path):
 configure(tmp_path);path=tmp_path/'resource-extension-approval.json';a=json.loads(path.read_text());a['max_requests']=4501;path.write_text(json.dumps(a))
 with pytest.raises(AssertionError):ExtendedBudget(tmp_path)
