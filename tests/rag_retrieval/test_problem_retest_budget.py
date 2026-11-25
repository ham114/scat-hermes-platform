"""Sync QA and async Ragas must share a hard, persistent admission limit."""
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def runner():
    path=Path(__file__).resolve().parents[2]/'scripts/run_problem_retest.py'
    spec=importlib.util.spec_from_file_location('problem_retest_budget_check',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_sync_and_async_requests_share_request_limit(tmp_path):
    m=runner()
    policy={'expires_at':time.time()+120,'max_requests':2,'max_output_tokens':4096,
            'max_conservative_units':100000,'max_returned_provider_tokens':100000}
    (tmp_path/'qa-policy.json').write_text(json.dumps(policy))
    qa=m.AggregateBudget(tmp_path,'case','qa')
    fake=lambda **kw:SimpleNamespace(usage={'prompt_tokens':10,'completion_tokens':5})
    qa.wrap_create(fake)(messages=[{'role':'user','content':'q'}],max_tokens=16)
    scorer=m.AsyncMeter(tmp_path,'case--faithfulness','ragas')
    admitted=scorer.admit({'messages':[{'role':'user','content':'score'}],'max_tokens':16})
    scorer.settle(admitted,{'prompt_tokens':20,'completion_tokens':5})
    with pytest.raises(RuntimeError,match='budget_exhausted'):
        scorer.admit({'messages':[],'max_tokens':16})
    assert qa.snapshot()['llm_calls']==2
    assert qa.snapshot()['provider_input_tokens']==30
    assert qa.snapshot()['provider_output_tokens']==10
    with pytest.raises(RuntimeError,match='campaign_budget_exhausted'):
        qa.wrap_create(fake)(messages=[],max_tokens=16)


def test_unknown_request_keeps_its_reservation(tmp_path):
    m=runner()
    (tmp_path/'qa-policy.json').write_text(json.dumps({'expires_at':time.time()+120,'max_requests':10,'max_output_tokens':4096,
        'max_conservative_units':100000,'max_returned_provider_tokens':100000}))
    meter=m.AsyncMeter(tmp_path,'case','ragas')
    admitted=meter.admit({'messages':[],'max_tokens':4096})
    before=meter.charge();meter.settle(admitted,None,'error_or_unknown')
    assert meter.charge()==before and meter.snapshot()['unknown_or_error_calls']==1
