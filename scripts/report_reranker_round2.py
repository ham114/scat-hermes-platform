"""Recompute evidence counts from saved permutations and export a reviewable report."""
import collections
import hashlib
import html
import json
import importlib.metadata
import sys
from pathlib import Path

from reranker_round2_run import OUT, OLD, SCHEMES, guard
from reranker_ft_common import ROOT, read, save, sha, BASE_EVAL


def main():
    guard()
    campaign=read(OUT/'campaign-summary.json')
    assert campaign['status']=='completed'
    phases=campaign['phases']
    assert all(a['end']<=b['begin'] for a,b in zip(phases,phases[1:]))
    plan=read(OUT/'frozen-plan.json')
    training_rows=[json.loads(x) for x in (OUT/'train.jsonl').read_text().splitlines()]
    profile=read(OUT/'final-data-profile.json')
    assert profile['queries']==len(training_rows)==plan['train_queries']
    assert profile['negative_pairs']==sum(len(g['negatives']) for g in training_rows)
    sample=read(OUT/'assistant-review.json')
    smoke=read(OUT/'smoke-checks.json')
    assert smoke['status']=='passed'
    original=read(OLD/'p1_pointwise/eval/summary.json')
    metrics={'untrained':{p['policy']['id']:p['untrained_same_policy_metrics'] for p in original['policies']}}
    gains={}
    actual_reference={r['id']:r for r in read(BASE_EVAL/'rank_rrf-results.json')['cases'] if r['cohort']=='old113'}
    cross_reference_losses={}
    canonical={u['id']:u for u in read(OUT/'dev-pools.json')+read(OUT/'test-pools.json')}
    main_baselines={}
    for pid in ['reranker','rank_rrf']:
        before={r['id']:r for r in read(BASE_EVAL/(pid+'-results.json'))['cases']}
        for u in canonical.values():
            if u['cohort']!='dev':continue
            scores=read(OUT/'dev-base-scores'/(u['id']+'.json'))['scores']
            positions={c:i for i,c in enumerate(u['pool'])}
            raw=sorted(u['pool'],key=lambda c:(-scores[c],positions[c]))
            ranks={c:i+1 for i,c in enumerate(raw)}
            ranking=raw if pid=='reranker' else sorted(u['pool'],key=lambda c:(-(4./(20+ranks[c])+1./(21+positions[c])),ranks[c],positions[c]))
            before[u['id']]={**u,'ranking':ranking}
        main_baselines[pid]=before
    checked=0
    adapter_checks={}
    names=['control_p1','control_p3','a_distill_preserve','b_graded_lambda','c_dual_branch']
    for sid in names:
        summary=read(OUT/sid/'eval/summary.json')
        training=read(OUT/sid/'training.json')
        assert training['status']=='completed' and training['train_queries']==len(training_rows)
        assert training['optimizer_steps']==10 and training['settings']==plan['settings']
        assert summary['status']=='completed' and summary['cohorts']=={'dev':72,'holdout120':120,'old113':113}
        adapter=Path(training['adapter_path'])
        assert all(sha(adapter/n)==h for n,h in training['adapter_files_sha256'].items())
        if sid!='c_dual_branch':
            from report_reranker_ft import verify_adapter
            adapter_checks[sid]=verify_adapter(adapter/'adapter_model.safetensors',training['trainable_parameters'])
        metrics[sid]={}
        gains[sid]={}
        cross_reference_losses[sid]={}
        score_cache={u:read(OUT/sid/'eval/scores'/(u+'.json'))['scores'] for u in canonical}
        for policy in summary['policies']:
            pid=policy['policy']['id']
            rows=read(OUT/sid/'eval'/(pid+'-results.json'))['cases']
            assert len(rows)==305 and len({r['id'] for r in rows})==305
            counts={}
            for cohort in ['dev','holdout120','old113']:
                subset=[r for r in rows if r['cohort']==cohort]
                counts[cohort]={str(k):{'complete_cases':sum(set(r['gold'])<=set(r['ranking'][:k]) for r in subset),
                                     'cases':len(subset)} for k in [5,10,20,50,100]}
            for row in rows:
                pool=row['pool']
                ranking=row['ranking']
                original=canonical[row['id']]
                assert pool==original['pool'] and row['gold']==original['gold']
                assert row['question']==original['question'] and row['cohort']==original['cohort']
                assert len(ranking)==len(set(ranking))==100 and set(ranking)==set(pool)
                scores=score_cache[row['id']]
                assert set(scores)==set(pool)
                positions={c:i for i,c in enumerate(pool)}
                raw=sorted(pool,key=lambda c:(-scores[c],positions[c]))
                if pid=='reranker':
                    assert ranking==raw
                elif pid=='rank_rrf':
                    rank={c:i+1 for i,c in enumerate(raw)}
                    expected=sorted(pool,key=lambda c:(-(4./(20+rank[c])+1./(20+positions[c]+1)),rank[c],positions[c]))
                    assert ranking==expected
                for k in [5,10,20,50,100]:
                    assert (set(row['gold'])<=set(ranking[:k]))==row['metrics'][str(k)]['all_evidence']
                checked+=1
            for cohort,depths in counts.items():
                for k,value in depths.items():
                    assert value['complete_cases']==policy['metrics'][cohort][k]['complete_cases']
            metrics[sid][pid]=counts
            gains[sid][pid]=policy['paired_against_untrained_same_policy']
            if pid in main_baselines:
                before=main_baselines[pid]
                recomputed={cohort:{str(k):{name:[r['id'] for r in rows if r['cohort']==cohort
                    and (set(r['gold'])<=set(r['ranking'][:k]))==gain
                    and (set(r['gold'])<=set(before[r['id']]['ranking'][:k]))!=gain]
                    for name,gain in [('gained',True),('lost',False)]} for k in [5,10]}
                    for cohort in ['dev','holdout120','old113']}
                assert gains[sid][pid]==recomputed
            cross_reference_losses[sid][pid]={str(k):[r['id'] for r in rows if r['cohort']=='old113'
                and set(r['gold'])<=set(actual_reference[r['id']]['ranking'][:k])
                and not set(r['gold'])<=set(r['ranking'][:k])] for k in [5,10]}
    proposal=read(OUT/'prepared-plan.json')
    validation={'status':'passed','ranking_metric_recomputations':checked,'adapter_checks':adapter_checks,
        'objective_unit_tests_passed':5,'gpu_objective_smokes':smoke,
        'serial_phases_nonoverlap':True,'fixed_candidate100_permutations_verified':True,
        'teacher_labels':'model-screened weak negatives, not independent expert labels',
        'inherited_negative_label_limit_field':'historical first-round annotation only; not read by the trainer. Current policy is defined by frozen-plan, negatives.kind and label_status.',
        'sample_review':sample,'production_changes':0,'external_api_calls':0}
    save(OUT/'metrics.json',metrics)
    save(OUT/'paired-gains-losses.json',gains)
    save(OUT/'old-historical-rrf-losses.json',cross_reference_losses)
    save(OUT/'delivery-validation.json',validation)
    target=ROOT/'output/reranker-round2-20261125'
    target.mkdir(parents=True,exist_ok=True)
    save(target/'dual-branch-latency.json',read(OUT/'c_dual_branch/latency.json'))
    save(target/'runtime-versions.json',{'python':sys.version,'prefix':sys.prefix,
         'packages':{n:importlib.metadata.version(n) for n in ['torch','transformers','peft','numpy','sentence-transformers']},
         'report_source_sha256':sha(Path(__file__))})
    for name in ['prepared-plan.json','frozen-plan.json','mining-summary.json','assistant-review.json',
                 'metrics.json','paired-gains-losses.json','old-historical-rrf-losses.json','delivery-validation.json','campaign-summary.json','final-data-profile.json','smoke-checks.json']:
        save(target/name,read(OUT/name))
    labels={'untrained':'未微调','control_p1':'同数据 P1 对照','control_p3':'同数据 P3 对照',
            'a_distill_preserve':'A 教师蒸馏＋保持','b_graded_lambda':'B 分级 LambdaLoss','c_dual_branch':'C 双分支融合'}
    body=[]
    for sid in ['untrained']+names:
        for pid in ['reranker','rank_rrf']:
            row=[labels[sid],pid]
            for cohort in ['dev','holdout120','old113']:
                row.append(' / '.join(str(metrics[sid][pid][cohort][str(k)]['complete_cases']) for k in [5,10]))
            row.append('0 / 0' if sid=='untrained' and pid=='rank_rrf' else
                       ('参照另列' if sid=='untrained' else ' / '.join(str(len(cross_reference_losses[sid][pid][str(k)])) for k in [5,10])))
            body.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>')
    losses=[]
    for sid in names:
        for pid in ['reranker','rank_rrf']:
            value=gains[sid][pid]['old113']
            losses.append({'scheme':sid,'policy':pid,'lost5':value['5']['lost'],'lost10':value['10']['lost']})
    save(target/'old-case-losses.json',losses)
    text=f'''<!doctype html><meta charset="utf-8"><title>第二轮重排串行实验</title>
<style>body{{font:16px/1.6 system-ui;max-width:1100px;margin:32px auto;padding:20px}}td,th{{border:1px solid #ccc;padding:8px}}table{{border-collapse:collapse}}code{{overflow-wrap:anywhere}}</style>
<h1>第二轮重排串行实验</h1>
<p>训练题 {plan['train_queries']}，独立项目背景题 {plan['background_queries']}。各训练方案从相同原始0.6B模型独立开始，P1/P3为同修订数据对照。A、B依次训练和评测；C复用A适配器与原模型做固定等权排名融合，没有第三次独立训练。</p>
<p>以下分子为Top5 / Top10找全标注证据的题数，不是问答准确率。验证72、新题120、旧题113。历史测试已经查看，不能称独立盲测，也没有用于检查点、阈值或融合权重选择。</p>
<table><tr><th>方案</th><th>后处理</th><th>验证72 Top5/10</th><th>新120 Top5/10</th><th>旧113 Top5/10</th><th>相对未微调RRF旧题新增丢失 Top5/10</th></tr>{''.join(body)}</table>
<p>已复算 {checked} 份排序及证据覆盖，并核对固定100候选与串行阶段。完整逐题得失见 <a href="paired-gains-losses.json">paired-gains-losses.json</a>；旧题退化见 <a href="old-case-losses.json">old-case-losses.json</a>。</p>
<p>同策略对照可能使用较弱基线。是否保留原有能力另以历史未微调RRF逐题对比，见 <a href="old-historical-rrf-losses.json">跨策略旧题参照</a>，不能只看净命中数或同策略无退化。</p>
<p>负例来自训练题实际Top100，由固定本地4B教师筛选；仍是模型弱标签，助手抽查不能替代专家全量确认。分级标签取自原证据组：单块支持全部组为2，只支持部分组为1，筛选负例为0。教师概率没有直接冒充完整/部分支持标注。</p>
<p>最终包含 {profile['positive_fragments']} 个正例片段、{profile['negative_pairs']} 个负例，其中 {profile['top100_hard_negatives']} 个来自训练题Top100同主题候选；这些候选并不全部是高置信度模型困难负例。背景保持样本仅5题，覆盖范围有限。<a href="final-data-profile.json">数据分布</a></p>
<p>A/B的排序损失作用于每题抽取的5–7个正负片段，不是完整100候选列表；B配置k=10，但在这些短列表内不会发生第10位截断。因此本轮只验证该短列表LambdaRank实现，不能据此确认完整Top100列表训练的效果。</p>
<p>本轮单seed、单epoch，不能据此确认稳定泛化或宣布一种损失普遍优越。Top100缺证据的问题不由重排修复；没有回答生成、付费模型API调用或生产切换。C输出为归一化排名分数，不是校准相关性概率；因此主要比较纯重排及基于排名的RRF，带分数阈值的MMR仅作附属观察。<a href="dual-branch-latency.json">双分支实际前向耗时</a>仅覆盖5道验证题，不是线上SLA。</p>
<p><a href="frozen-plan.json">冻结计划</a> · <a href="mining-summary.json">数据筛选结果</a> · <a href="assistant-review.json">抽查记录</a> · <a href="delivery-validation.json">交付复核</a></p>'''
    (target/'REPORT.html').write_text(text,encoding='utf8')
    print(json.dumps({'report':str(target/'REPORT.html'),'verified_rankings':checked,
                      'metrics':{s:{p:{c:[v[c][str(k)]['complete_cases'] for k in [5,10]] for c in ['dev','holdout120','old113']} for p,v in ps.items() if p in ['reranker','rank_rrf']} for s,ps in metrics.items()}},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
