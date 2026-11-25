"""Serial, offline second-round controls, distillation, LambdaLoss and fusion.

Reuses the first experiment's audited trainer/evaluator without editing them.
The new output directory and frozen checks are installed only in this process.
"""
import argparse
import copy
import gc
import json
import math
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import reranker_ft_common as common
from reranker_ft_common import ROOT, read, save, sha, offline, no_generation

OUT = ROOT / 'var/validation/20261125-reranker-round2'
OLD = ROOT / 'var/validation/20261125-reranker-lora-v1'
SCHEMES = [
    {'id':'control_p1','objective':'pointwise','priority':0},
    {'id':'control_p3','objective':'pairwise','priority':0},
    {'id':'a_distill_preserve','objective':'distill','priority':1},
    {'id':'b_graded_lambda','objective':'lambda','priority':2},
]
PY = '/home/h3c/anaconda3/envs/scat-rag-train/bin/python'


def lambda_loss(scores, grades, k=10):
    """Pairwise logistic loss weighted by absolute predicted-position delta NDCG@k."""
    import torch
    import torch.nn.functional as F
    y = torch.as_tensor(grades, dtype=scores.dtype, device=scores.device)
    gains = torch.pow(2., y)-1.
    with torch.no_grad():
        order = torch.argsort(scores, descending=True, stable=True)
        ranks = torch.empty_like(order)
        ranks[order] = torch.arange(len(order), device=scores.device)
        discounts = torch.where(ranks<k, 1./torch.log2(ranks.float()+2.), 0.)
        ideal = torch.sort(gains, descending=True).values
        ideal_discount = 1./torch.log2(torch.arange(len(y),device=scores.device).float()+2.)
        ideal_discount[k:] = 0.
        idcg = (ideal*ideal_discount).sum().clamp_min(1e-8)
        weight = ((gains[:,None]-gains[None,:])*(discounts[:,None]-discounts[None,:])).abs()/idcg
        valid = y[:,None]>y[None,:]
        weight *= valid
    cost = F.softplus(-(scores[:,None]-scores[None,:]))
    return (cost*weight).sum()/weight.sum().clamp_min(1e-8)


def distill_loss(scores, positive_count, teacher_probabilities, original_probabilities, background):
    import torch
    import torch.nn.functional as F
    binary = common.objective(scores, positive_count, 'pointwise')
    teacher = torch.tensor(teacher_probabilities, device=scores.device).clamp(.001,.999)
    # Explicit evidence annotations take precedence over teacher false negatives.
    teacher[:positive_count] = teacher[:positive_count].clamp_min(.5)
    teacher_logits = torch.logit(teacher)
    soft = F.kl_div(F.log_softmax(scores/2.,dim=0), F.softmax(teacher_logits/2.,dim=0), reduction='sum')*4.
    preserve = scores.sum()*0.
    if background:
        original = torch.tensor(original_probabilities,device=scores.device).clamp(.001,.999)
        preserve = (F.binary_cross_entropy_with_logits(scores, original, reduction='none') +
                    original*original.log()+(1-original)*(1-original).log()).mean()
    return .4*binary+.4*soft+.2*preserve


def guard():
    p = read(OUT/'frozen-plan.json')
    assert all(sha(ROOT/n)==h for n,h in p['protected_sha256'].items()), 'frozen input/source modified'


def freeze():
    assert not (OUT/'frozen-plan.json').exists()
    preparation = read(OUT/'prepared-plan.json')
    mining = read(OUT/'mining-summary.json')
    review = read(OUT/'assistant-review.json')
    test_log=(OUT/'objective-tests.log').read_text()
    assert 'Ran 5 tests' in test_log and '\nOK' in test_log
    assert mining['status']=='awaiting_assistant_sample_review'
    assert review['status']=='passed' and review['sample_sha256']==sha(OUT/'negative-review.json')
    teacher_manifest=ROOT/'services/rag/qwen3-4b-download.json'
    assert all(read(p)['teacher_manifest_sha256']==sha(teacher_manifest)
               for p in (OUT/'teacher').glob('*.json'))
    rejected = {(x['id'],x['negative_id']) for x in review.get('rejected_pairs',[])}
    train = []
    for row in map(json.loads,(OUT/'train-proposed.jsonl').read_text().splitlines()):
        keep = [i for i,n in enumerate(row['negatives']) if (row['id'],n['chunk_id']) not in rejected]
        count = len(row['gold'])
        original_indices = list(range(count))+[count+i for i in keep]
        negatives = [row['negatives'][i] for i in keep]
        hard = [count+j for j,n in enumerate(negatives) if n['kind']=='teacher_screened_same_topic_top100']
        if len(negatives)<2 or not hard:
            continue
        for name in ['grades','teacher_probabilities','original_probabilities']:
            row[name] = [row[name][i] for i in original_indices]
        row['negatives'] = negatives
        row['hard_negative_positions'] = hard
        row['admission'] = 'admitted_after_semantic_isolation_teacher_screening_and_assistant_sample_review'
        train.append(row)
    assert len(train)>=60 and sum(g['background'] for g in train)>0
    assert len({g['question'] for g in train})==len(train)
    (OUT/'train.jsonl').write_text(''.join(json.dumps(g,ensure_ascii=False)+'\n' for g in train),encoding='utf8')
    for name in ['dev-pools.json','test-pools.json']:
        save(OUT/name,read(OLD/name))
    # Reuse frozen untrained dev scores; they identify exactly the same query/pool.
    import shutil
    shutil.copytree(OLD/'dev-base-scores',OUT/'dev-base-scores',dirs_exist_ok=True)
    protected = dict(preparation['source_hashes'])
    protected[teacher_manifest.relative_to(ROOT).as_posix()]=sha(teacher_manifest)
    for p in list(common.BASE_EVAL.glob('*-results.json')) + list((common.BASE_EVAL/'shared').glob('*.json')) + list((OUT/'dev-base-scores').glob('*.json')):
        protected[p.relative_to(ROOT).as_posix()]=sha(p)
    p=OLD/'p1_pointwise/eval/summary.json'
    protected[p.relative_to(ROOT).as_posix()]=sha(p)
    for name in ['train.jsonl','dev-pools.json','test-pools.json','assistant-review.json','negative-review.json',
                 'mining-summary.json','prepared-plan.json','objective-tests.log']:
        p = OUT/name
        protected[p.relative_to(ROOT).as_posix()] = sha(p)
    for p in [ROOT/'scripts/reranker_round2_run.py',ROOT/'scripts/reranker_round2_data.py',
              ROOT/'scripts/train_reranker_ft.py',ROOT/'scripts/evaluate_reranker_ft.py',
              ROOT/'scripts/reranker_ft_common.py',ROOT/'scripts/rerank_postprocess_policies.py']:
        protected[p.relative_to(ROOT).as_posix()]=sha(p)
    # Existing runtime, corpus, vectors and model manifest remain protected.
    protected.update({n:h for n,h in read(OLD/'plan.json')['protected_sha256'].items()
                      if n.startswith(('src/','services/','var/rag/'))})
    plan = {'status':'frozen','schemes':SCHEMES,'fusion':{'id':'c_dual_branch','adapter':'a_distill_preserve',
             'k':20,'original_weight':.5,'adapted_weight':.5},'settings':common.SETTINGS,
             'train_queries':len(train),'background_queries':sum(g['background'] for g in train),
             'negative_pairs':sum(len(g['negatives']) for g in train),'protected_sha256':protected,
             'objectives':{'a_distill_preserve':{'binary_weight':.4,'teacher_list_kl_weight':.4,
                  'original_bernoulli_kl_weight':.2,'distillation_temperature':2.,
                  'preservation_scope':'independent project-background training rows only',
                  'annotated_positive_teacher_probability_floor':.5},
                  'b_graded_lambda':{'binary_weight':.3,'delta_ndcg_logistic_weight':.7,'k':10,
                  'grade_policy':'2 if annotated chunk alone covers all groups, 1 if partial evidence, 0 if screened negative'}},
             'data_sha256':{n:sha(OUT/n) for n in ['train.jsonl','dev-pools.json','test-pools.json']},
             'checkpoint':'fixed_one_epoch_last; dev determines eligibility; no test-driven selection',
             'comparison':'revised-data controls plus three serial experiments; fixed previously viewed tests',
             'external_model_calls':0,'generation_calls':0,'production_changes':0,'frozen_at':time.time()}
    save(OUT/'frozen-plan.json',plan)
    print(json.dumps({'frozen':True,'train_queries':len(train),'background_queries':plan['background_queries']}),flush=True)


def configure_training():
    import train_reranker_ft as trainer
    trainer.OUT = OUT
    trainer.SCHEMES = SCHEMES
    trainer.check_protected = guard
    trainer.SETTINGS = dict(common.SETTINGS)
    groups = {g['question']:g for g in map(json.loads,(OUT/'train.jsonl').read_text().splitlines())}
    original_batch = trainer.batch_scores
    original_objective = trainer.objective
    state = {}
    def batch(*args, **kwargs):
        state['group'] = groups[args[2][0]]
        return original_batch(*args,**kwargs)
    def objective(scores, count, kind, hard=None):
        g = state['group']
        if kind=='distill':
            return distill_loss(scores,count,g['teacher_probabilities'],g['original_probabilities'],g['background'])
        if kind=='lambda':
            return .3*original_objective(scores,count,'pointwise')+.7*lambda_loss(scores,g['grades'])
        return original_objective(scores,count,kind,hard)
    trainer.batch_scores = batch
    trainer.objective = objective
    return trainer


def train(sid):
    guard()
    trainer = configure_training()
    sys.argv = ['train_reranker_ft.py','--scheme',sid]
    trainer.main()


def smoke():
    guard()
    import torch
    rows=[json.loads(x) for x in (OUT/'train.jsonl').read_text().splitlines()]
    chosen=[next(g for g in rows if not g['background']),next(g for g in rows if g['background'])]
    trainer=configure_training()
    trainer.SETTINGS={**common.SETTINGS,'gradient_accumulation_queries':1}
    trainer.SCHEMES=[{'id':'smoke_distill','objective':'distill'}, {'id':'smoke_lambda','objective':'lambda'}]
    results={}
    for spec in trainer.SCHEMES:
        directory=OUT/'smoke'/spec['id']
        directory.mkdir(parents=True,exist_ok=True)
        (directory/'train.jsonl').write_text(''.join(json.dumps(g,ensure_ascii=False)+'\n' for g in chosen),encoding='utf8')
        trainer.OUT=directory
        completed=directory/spec['id']/'training.json'
        if not completed.exists():
            sys.argv=['train_reranker_ft.py','--scheme',spec['id']]
            trainer.main()
        result=read(completed)
        assert result['optimizer_steps']==2 and result['gradient_norm_max']>0
        results[spec['id']]={'optimizer_steps':2,'gradient_norm_max':result['gradient_norm_max'],
                             'training_sha256':sha(completed)}
        gc.collect()
        torch.cuda.empty_cache()
    save(OUT/'smoke-checks.json',{'status':'passed','objectives':results,'production_changes':0})


def evaluate(sid):
    guard()
    import train_reranker_ft as trainer
    trainer.OUT = OUT
    import evaluate_reranker_ft as evaluator
    evaluator.OUT = OUT
    evaluator.check_protected = guard
    sys.argv = ['evaluate_reranker_ft.py','--scheme',sid]
    evaluator.main()


def fuse():
    guard()
    from train_reranker_ft import load_scorer
    import train_reranker_ft as trainer
    trainer.OUT = OUT
    import torch
    sid = 'c_dual_branch'
    trained = read(OUT/'a_distill_preserve/training.json')
    target = OUT/sid
    save(target/'training.json',{**trained,'scheme':sid,'training_reused_from':'a_distill_preserve','new_training_steps':0,
         'fusion_score_semantics':'rescaled rank score, not a calibrated relevance probability',
         'primary_policies':['reranker','rank_rrf']})
    cases = read(OUT/'dev-pools.json')+read(OUT/'test-pools.json')
    cfg,chunks,_ = common.corpus()
    base_directory = common.BASE_EVAL/'shared'
    assert base_directory.exists(), 'Historical raw baseline scores are required'
    identity = {'adapter_files':trained['adapter_files_sha256'],'dev_pools':sha(OUT/'dev-pools.json'),
                'test_pools':sha(OUT/'test-pools.json')}
    for u in cases:
        adapted = read(OUT/'a_distill_preserve/eval/scores'/(u['id']+'.json'))
        if u['cohort']=='dev':
            base = read(OUT/'dev-base-scores'/(u['id']+'.json'))['scores']
        else:
            base = read(base_directory/(u['id']+'.json'))['scores']
        pool = u['pool']
        assert set(base)==set(adapted['scores'])==set(pool)
        order = {c:i for i,c in enumerate(pool)}
        ranks = []
        for scores in [base,adapted['scores']]:
            ranked = sorted(pool,key=lambda c:(-scores[c],order[c]))
            ranks.append({c:i+1 for i,c in enumerate(ranked)})
        # Rescale to [0,1] to satisfy existing postprocessor score contracts.
        fused = {c:21*(.5/(20+ranks[0][c])+.5/(20+ranks[1][c])) for c in pool}
        save(target/'eval/scores'/(u['id']+'.json'),{**u,'scores':fused,'identity':identity,
             'seconds':0,'info':{'truncated_pairs':0,'cached_rank_fusion':True}})
    # Measure actual two-branch execution on five dev cases, not cached fusion time.
    r = load_scorer(Path(trained['adapter_path']))
    times = []
    for u in cases[:5]:
        docs = [chunks[c].retrieval_text() for c in u['pool']]
        torch.cuda.synchronize()
        started = time.perf_counter()
        with r.model.disable_adapter():
            r.score(u['question'],docs)
        r.score(u['question'],docs)
        torch.cuda.synchronize()
        times.append(time.perf_counter()-started)
    save(target/'latency.json',{'actual_two_branch_queries':5,'forward_pairs':1000,
         'seconds':times,'p50_seconds':statistics.median(times),'scope':'small dev-only latency sample; not production SLA'})
    del r
    gc.collect()
    torch.cuda.empty_cache()


def campaign():
    import fcntl
    with (OUT/'campaign.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        guard()
        if not (OUT/'smoke-checks.json').exists():
            save(OUT/'campaign-progress.json',{'stage':'gpu_objective_smoke'})
            subprocess.run([PY,__file__,'smoke'],cwd=ROOT,check=True)
        phases=[]
        for scheme in SCHEMES:
            sid=scheme['id']
            started=time.time()
            for stage,done in [('train','training.json'),('evaluate','eval/summary.json')]:
                save(OUT/'campaign-progress.json',{'scheme':sid,'stage':stage})
                if not (OUT/sid/done).exists():
                    subprocess.run([PY,__file__,stage,'--scheme',sid],cwd=ROOT,check=True)
            phases.append({'scheme':sid,'begin':started,'end':time.time()})
            save(OUT/'completed-phases.json',phases)
        started=time.time()
        sid='c_dual_branch'
        save(OUT/'campaign-progress.json',{'scheme':sid,'stage':'fuse_and_evaluate'})
        if not (OUT/sid/'eval/summary.json').exists():
            subprocess.run([PY,__file__,'fuse'],cwd=ROOT,check=True)
            subprocess.run([PY,__file__,'evaluate','--scheme',sid],cwd=ROOT,check=True)
            p=OUT/sid/'eval/summary.json'
            d=read(p)
            d.update(fresh_forward_pairs=1000, cached_fusion_queries=305, training_reused_from='a_distill_preserve')
            save(p,d)
        phases.append({'scheme':sid,'begin':started,'end':time.time()})
        assert all(a['end']<=b['begin'] for a,b in zip(phases,phases[1:]))
        guard()
        save(OUT/'campaign-summary.json',{'status':'completed','phases':phases,
             'external_llm_calls':0,'generation_calls':0,'production_changes':0})


def main():
    p=argparse.ArgumentParser()
    p.add_argument('stage',choices=['freeze','train','evaluate','fuse','smoke','campaign'])
    p.add_argument('--scheme')
    a=p.parse_args()
    offline()
    no_generation()
    if a.stage=='train':train(a.scheme)
    elif a.stage=='evaluate':evaluate(a.scheme)
    else:globals()[a.stage]()


if __name__=='__main__':
    main()
