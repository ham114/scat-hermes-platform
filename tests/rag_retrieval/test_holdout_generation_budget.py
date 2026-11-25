import json
import sys
import time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from ragas_aviation_budget import CampaignBudget


def budget(folder,**overrides):
    folder.mkdir(parents=True,exist_ok=True)
    policy={'started_at':time.time(),'expires_at':time.time()+60,'max_requests':2,
            'max_provider_tokens':100000,'max_conservative_units':100000,**overrides}
    (folder/'policy.json').write_text(json.dumps(policy))
    return CampaignBudget(folder)


def test_unknown_failure_keeps_reservation_and_consumes_request(tmp_path):
    b=budget(tmp_path/'new',max_requests=1)
    request=b.admit('q','generation',[{'role':'user','content':'public excerpt'}],2400)
    before=b.snapshot();b.settle(request,error=RuntimeError('failure'))
    after=b.snapshot();assert after['requests']==1 and after['provider_budget_charge']==before['provider_budget_charge']
    with pytest.raises(RuntimeError,match='budget_exhausted'):b.admit('q2','review',[],2400)


def test_expiry_and_token_cap_block_before_request(tmp_path):
    for name,kw in [('expired',{'expires_at':time.time()-1}),('tokens',{'max_provider_tokens':100})]:
        b=budget(tmp_path/name,**kw)
        with pytest.raises(RuntimeError,match='budget_exhausted'):b.admit('q','generation',[],2400)
        assert b.snapshot()['requests']==0


def test_new_campaign_does_not_reset_or_change_old_ledger(tmp_path):
    old=budget(tmp_path/'old');old.admit('old','generation',[],2400)
    old_snapshot=old.snapshot()
    new=budget(tmp_path/'new');assert new.snapshot()['requests']==0
    new.admit('new','generation',[],2400)
    assert old.snapshot()==old_snapshot and new.snapshot()['requests']==1
