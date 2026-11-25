import hashlib,json,sys,time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from ragas_aviation_budget import CampaignBudget
from ragas_holdout_budget_extension import ExtendedBudget


def create_policy_case(folder):
    policy={'started_at':time.time(),'expires_at':time.time()+60,'max_requests':1200,
            'max_provider_tokens':8000000,'max_conservative_units':40000000}
    p=folder/'policy.json';p.write_text(json.dumps(policy))
    approval={'authorized':True,'max_requests':3000,'original_policy_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
              **{k:policy[k] for k in ['expires_at','max_provider_tokens','max_conservative_units']}}
    (folder/'request-extension-approval.json').write_text(json.dumps(approval))
    return policy,approval


def test_extension_preserves_original_policy_and_consumption(tmp_path):
    policy,_=create_policy_case(tmp_path);old=CampaignBudget(tmp_path)
    r=old.admit('old','generation',[],2400);old.settle(r,error=RuntimeError())
    before=old.snapshot();digest=hashlib.sha256((tmp_path/'policy.json').read_bytes()).hexdigest()
    b=ExtendedBudget(tmp_path);after=b.snapshot()
    for k in ['requests','conservative_units','provider_budget_charge']:assert before[k]==after[k]
    assert b.policy['max_requests']==3000 and b.policy['expires_at']==policy['expires_at']
    assert hashlib.sha256((tmp_path/'policy.json').read_bytes()).hexdigest()==digest
    assert ExtendedBudget(tmp_path).snapshot()==after


def test_other_limits_cannot_be_changed_under_request_extension(tmp_path):
    _,approval=create_policy_case(tmp_path);approval['max_provider_tokens']+=1
    (tmp_path/'request-extension-approval.json').write_text(json.dumps(approval))
    with pytest.raises(AssertionError):ExtendedBudget(tmp_path)
