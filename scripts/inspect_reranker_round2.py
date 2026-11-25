"""Compact, read-only progress for the authorized serial experiment."""
import json
import os
import sys
import time
from pathlib import Path

root=Path(__file__).resolve().parents[1]/'var/validation/20261125-reranker-round2'
def read(path):
    return json.loads(path.read_text(encoding='utf8'))

output={'observed_at':time.time()}
for name in ['campaign-progress.json','campaign-summary.json']:
    p=root/name
    if p.exists():output[name]=read(p)
launch=root/'campaign-launch.json'
if launch.exists():
    pid=read(launch)['pid']
    try:
        os.kill(pid,0)
        output['campaign_pid_alive']=True
    except OSError:
        output['campaign_pid_alive']=False
for sid in ['control_p1','control_p3','a_distill_preserve','b_graded_lambda','c_dual_branch']:
    folder=root/sid
    if not folder.exists():continue
    value={}
    for name in ['progress.json','eval/progress.json']:
        p=folder/name
        if p.exists():value[name]=read(p)
    summary=folder/'eval/summary.json'
    if summary.exists():
        s=read(summary)
        value['completed_metrics']={p['policy']['id']:{h:[p['metrics'][h][str(k)]['complete_cases'] for k in [5,10]]
                                    for h in ['dev','holdout120','old113']} for p in s['policies']
                                    if p['policy']['id'] in ['reranker','rank_rrf']}
    output[sid]=value
if '--compact' in sys.argv:
    phase=output.get('campaign-progress.json',{})
    names=['control_p1','control_p3','a_distill_preserve','b_graded_lambda','c_dual_branch']
    result={'phase':phase,'alive':output.get('campaign_pid_alive'),
            'completed':[n for n in names if 'completed_metrics' in output.get(n,{})]}
    current=output.get(phase.get('scheme'),{})
    result['progress']=current.get('eval/progress.json',current.get('progress.json'))
    if (root/'campaign-summary.json').exists():result['status']='completed'
    if output.get('campaign_pid_alive') is False and result.get('status')!='completed':
        result['error_tail']=(root/'campaign.log').read_text()[-1400:]
    output=result
print(json.dumps(output,ensure_ascii=False))
