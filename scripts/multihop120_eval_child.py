"""One isolated fast native RAG turn, optionally with actual prior text history."""
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'vendor/hermes-agent'),str(ROOT/'scripts')]
from aviation300_qa_meter import AggregateBudget,process_dns


def main():
    os.umask(0o077)
    request_path=Path(sys.argv[1]).resolve()
    assert request_path.is_relative_to(ROOT/'var/validation/20261124-rag-multihop120-eval')
    request=json.loads(request_path.read_text())
    assert set(request)=={'case_id','question','actual_history','output','inspect_only'},'answer_request_must_not_include_gold'
    folder=ROOT/'var/validation/20261124-rag-multihop120-eval'
    meter=AggregateBudget(folder/'preflight' if request.get('inspect_only') else folder,request['case_id'],'qa')
    process_dns(meter.policy)
    config=json.loads((ROOT/'services/rag/real-agent-production-v1.json').read_text())
    config['scope']='aviation300-20261124-'+request['case_id']
    config['run_budget']={'max_turns':18,'max_llm_tokens':750000,'max_tool_calls':45,'wall_seconds':240}
    config['mode']='fast'
    import run_agent
    original=run_agent.AIAgent.run_conversation
    history=request.get('actual_history',[])
    assert all(set(row)=={'role','content'} and row['role'] in {'user','assistant'} for row in history)
    def conversation(agent,*args,**kwargs):
        if history:
            if kwargs.get('conversation_history'):raise ValueError('unexpected_preexisting_history')
            kwargs['conversation_history']=history
        return original(agent,*args,**kwargs)
    run_agent.AIAgent.run_conversation=conversation
    from hermes_runtime.real_rag_native import run_question
    output=Path(request['output']).resolve()
    assert output.is_relative_to(folder)
    result=run_question(ROOT,config,request['question'],'fast',output,meter,inspect_only=request.get('inspect_only',False))
    result.update(history_protocol='actual_prior_user_and_assistant_text_only' if history else 'single_turn',
                  actual_history_sha256=hashlib.sha256(json.dumps(history,sort_keys=True).encode()).hexdigest(),
                  production_same_source_isolated_native_execution=True)
    path=output/'result.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'case_id':request['case_id'],'status':result['status'],'history_messages':len(history),'budget':meter.snapshot()}),flush=True)


if __name__=='__main__':main()
