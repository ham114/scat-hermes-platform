"""Freeze data, three settings and test candidate universes after smoke checks."""
import collections,importlib.metadata,time
from reranker_ft_common import *
def main():
 offline();assert not (OUT/'plan.json').exists(),'already frozen'
 draft=read(OUT/'draft-plan.json');assert all(sha(ROOT/n)==h for n,h in draft['protected_sha256'].items())
 assert all(sha(OUT/n)==h for n,h in draft['data_sha256'].items())
 smoke=read(OUT/'smoke-v3/training.json');assert smoke['status']=='completed'and smoke['smoke']['all_three_losses_finite']and smoke['smoke']['lora_only_gradients']
 assert read(OUT/'smoke-v3/eval/summary.json')['status']=='completed'
 review=read(OUT/'negative-sample-review.json');assert review['status']=='passed'
 assert review['sample_sha256']==sha(OUT/'negative-review-sample.json')
 assert read(OUT/'preflight-validation.json')['unit_tests']==13
 pairs=[json.loads(l)for l in (OUT/'ragfit-pairs.jsonl').read_text(encoding='utf8').splitlines()]
 processed=[json.loads(l)for l in (OUT/'ragfit-processed/relevance-main.jsonl').read_text(encoding='utf8').splitlines()]
 assert len(pairs)==len(processed)==draft['ragfit_rows']
 template=(OUT/'reranker-prompt.txt').read_text(encoding='utf8')
 for a,b in zip(pairs,processed,strict=True):
  assert b['prompt']==template.format(query=a['query'],document=a['document']) and a['answer']==b['answer']in ['yes','no']
 tests=[]
 for u in read(BASE_EVAL/'dataset.json')['cases']:
  pool=read(BASE_EVAL/'shared'/(u['id']+'.json'))['pool'];assert len(pool)==100
  tests.append({**u,'pool':pool})
 assert collections.Counter(u['cohort']for u in tests)=={'holdout120':120,'old113':113}
 save(OUT/'test-pools.json',tests)
 # Every known held-out gold/alias stays outside supervised pairs.
 protected={c for u in tests for c in u['gold']}
 for v in read(BASE_EVAL/'quote-compatible-alternatives.json').values():protected.update(v)
 all_groups=[json.loads(l)for n in ['train','dev']for l in (OUT/(n+'.jsonl')).read_text(encoding='utf8').splitlines()]
 all_positive_aliases={c for g in all_groups for c in g['equivalent_ids']}
 for g in all_groups:
  assert not set(g['equivalent_ids'])&protected
  assert not {n['chunk_id']for n in g['negatives']}&protected
  assert set(g['gold']).isdisjoint(n['chunk_id']for n in g['negatives'])
  assert not {n['chunk_id']for n in g['negatives']}&all_positive_aliases
 train_alias={c for g in all_groups if g['split']=='train'for c in g['equivalent_ids']};dev_alias={c for g in all_groups if g['split']=='dev'for c in g['equivalent_ids']};assert not train_alias&dev_alias
 scripts=['reranker_ft_common.py','prepare_reranker_ft.py','train_reranker_ft.py','evaluate_reranker_ft.py','run_reranker_ft_campaign.py','freeze_reranker_ft.py','run_ragfit_reranker_processing.py','validate_and_start_reranker_ft.py']
 protected_files={**draft['protected_sha256'],**{('scripts/'+n):sha(ROOT/'scripts'/n)for n in scripts},
 **{(OUT/n).relative_to(ROOT).as_posix():sha(OUT/n)for n in [*draft['data_sha256'],'test-pools.json','negative-sample-review.json']}}
 p={**draft,'status':'frozen','frozen_at':time.time(),'settings':SETTINGS,'schemes':SCHEMES,'protected_sha256':protected_files,
  'packages':{n:importlib.metadata.version(n)for n in ['torch','transformers','peft','accelerate']},'ragfit_native_rows_verified':len(processed),'smoke_adapter_not_for_promotion':True,
  'selection_rule':'All three one-epoch adapter-last models evaluated. Dev raw Top5 and Top10 must not fall for eligibility; report all held-out results, do not tune/select checkpoints using holdout120; old113 per-case regression is a separate promotion guard. No production promotion in this campaign.',
  'user_authorization':'准备完成后，按上述优先级直接串行训练及复测','new_questions_generated':0,'qa_calls':0,'external_llm_calls':0,'generation_calls':0,'production_changes':0}
 p['unit_tests_passed']=13
 p['objectives']={'p1_pointwise':'weighted binary CE on original yes/no logits; all annotated evidence positive, screened negatives weight0.5',
 'p2_multi_positive':'0.3 binary CE + 0.7 mean over every positive of (logsumexp(all scores)-positive score)',
 'p3_pairwise':'0.3 binary CE + 0.7 softplus(0.2-weakest positive+retrieval-hard screened negative), retrieval-hard is not a confirmed model-hard label'}
 cfg=read(ROOT/'services/rag/qwen3-production-v1.json');manifest=read(ROOT/cfg['model_manifest'])
 external=[Path(manifest['destination'])/r['path']for r in manifest['files']]
 external += [Path('/home/h3c/project/python/RAG-FiT')/n for n in ['processing.py','training.py','pyproject.toml']]
 p['external_protected_sha256']={str(f):sha(f)for f in external}
 for n in ['rerank_comparison.py','enhancements.py']:p['protected_sha256']['src/rag_retrieval/'+n]=sha(ROOT/'src/rag_retrieval'/n)
 save(OUT/'plan.json',p);print(json.dumps({'frozen':True,'train_queries':p['train_queries'],'dev_queries':p['dev_queries'],'schemes':SCHEMES,'ragfit_native_rows_verified':len(processed)}),flush=True)
if __name__=='__main__':main()
