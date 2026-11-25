"""Compact read-only status of the authorized offline campaign."""
import json, time
from pathlib import Path

root = Path(__file__).resolve().parents[1] / 'var/validation/20261125-reranker-lora-v1'
read = lambda p: json.loads(p.read_text(encoding='utf8'))
output = {'observed_at':time.time()}
for n in ['campaign-progress.json','campaign-summary.json']:
    if (root/n).exists(): output[n] = read(root/n)
for sid in ['p1_pointwise','p2_multi_positive','p3_pairwise']:
    p = root/sid
    if not p.exists(): continue
    entry = {}
    for n in ['progress.json','eval/progress.json']:
        if (p/n).exists(): entry[n] = read(p/n)
    if (p/'eval/summary.json').exists():
        s = read(p/'eval/summary.json')
        entry['dev_eligible'] = s['dev_raw_top5_top10_not_worse']
        entry['results'] = [{'policy':ps['policy']['id'],
          'complete_top5_top10':{h:[ps['metrics'][h][str(k)]['complete_cases'] for k in [5,10]] for h in ['dev','holdout120','old113']},
          'old113_top5_top10_losses':[len(ps['paired_against_untrained_same_policy']['old113'][str(k)]['lost']) for k in [5,10]]}
          for ps in s['policies']]
    output[sid] = entry
print(json.dumps(output,ensure_ascii=False))
