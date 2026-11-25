"""Hard shared limits must survive a new process/controller instance."""
import importlib.util,json,time
from pathlib import Path
from types import SimpleNamespace
import pytest

def runner():
 p=Path(__file__).resolve().parents[2]/'scripts/run_multihop120_eval.py'
 spec=importlib.util.spec_from_file_location('multihop120_budget_check',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_sync_async_and_reopened_controller_share_limit(tmp_path):
 m=runner();(tmp_path/'qa-policy.json').write_text(json.dumps({'expires_at':time.time()+60,'max_requests':2,'max_output_tokens':4096,'max_conservative_units':100000,'max_returned_provider_tokens':100000}))
 sync=m.AggregateBudget(tmp_path,'q','qa');sync.wrap_create(lambda **kw:SimpleNamespace(usage={'prompt_tokens':10,'completion_tokens':5}))(messages=[],max_tokens=16)
 asynchronous=m.AsyncMeter(tmp_path,'q','ragas');token=asynchronous.admit({'messages':[],'max_tokens':16});asynchronous.settle(token,None,'error_or_unknown');reserved=asynchronous.charge()
 reopened=m.AsyncMeter(tmp_path,'new-controller','ragas');assert reopened.snapshot()['llm_calls']==2 and reopened.charge()==reserved
 with pytest.raises(RuntimeError,match='budget_exhausted'):reopened.admit({'messages':[],'max_tokens':16})
 assert reopened.snapshot()['llm_calls']==2
 with pytest.raises(RuntimeError,match='campaign_budget_exhausted'):sync.wrap_create(lambda **kw:None)(messages=[],max_tokens=16)

def test_expired_campaign_admits_no_requests(tmp_path):
 m=runner();(tmp_path/'qa-policy.json').write_text(json.dumps({'expires_at':time.time()-1,'max_requests':4000,'max_output_tokens':4096,'max_conservative_units':160000000,'max_returned_provider_tokens':24000000}))
 meter=m.AsyncMeter(tmp_path,'q','ragas')
 with pytest.raises(RuntimeError,match='budget_exhausted'):meter.admit({'messages':[],'max_tokens':16})
 assert meter.snapshot()['llm_calls']==0
