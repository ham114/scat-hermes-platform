"""Independently replay every frozen ranking, verify adapters, deliver a local report."""
import collections, hashlib, html, json, shutil, struct, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
RAW = ROOT / 'var/validation/20261125-reranker-lora-v1'
BASE = ROOT / 'var/validation/20261125-rag-holdout120-rerank-serial-v1'
OUT = ROOT / 'output/reranker-lora-20261125'
IDS = ['p1_pointwise', 'p2_multi_positive', 'p3_pairwise']
NAMES = {'untrained': '原始模型', 'p1_pointwise': 'P1 二分类 LoRA',
         'p2_multi_positive': 'P2 多正例列表 LoRA', 'p3_pairwise': 'P3 弱正例配对 LoRA'}
DEPTHS = [5, 10, 20, 50, 100]
read = lambda p: json.loads(p.read_text(encoding='utf8'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
esc = lambda s: html.escape(str(s), quote=True)


def save(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')


def aggregate(rows):
    return {h: {str(k): {'complete_cases': sum(r['metrics'][str(k)]['all_evidence'] for r in rs),
                        'cases': len(rs), 'evidence_hits': sum(r['metrics'][str(k)]['evidence_hits'] for r in rs),
                        'evidence_total': sum(len(r['gold']) for r in rs)} for k in DEPTHS}
            for h in ['dev', 'holdout120', 'old113'] for rs in [[r for r in rows if r['cohort'] == h]]}


def pairs(rows, before):
    return {h: {str(k): {name: [r['id'] for r in rows if r['cohort'] == h
                  and r['metrics'][str(k)]['all_evidence'] == gain
                  and before[r['id']]['metrics'][str(k)]['all_evidence'] != gain]
                  for name, gain in [('gained', True), ('lost', False)]} for k in [5, 10]}
            for h in ['dev', 'holdout120', 'old113']}


def verify_adapter(path, expected_parameters):
    import numpy as np
    data = path.read_bytes()
    length = struct.unpack('<Q', data[:8])[0]
    header = json.loads(data[8:8+length])
    total = 0
    nonzero_b = 0
    tensors = 0
    for name, row in header.items():
        if name == '__metadata__': continue
        assert 'lora_A' in name or 'lora_B' in name
        assert row['dtype'] == 'F32'
        start, end = row['data_offsets']
        values = np.frombuffer(data[8+length+start:8+length+end], dtype='<f4')
        assert len(values) == int(np.prod(row['shape'])) and np.isfinite(values).all()
        if 'lora_B' in name: nonzero_b += int(np.count_nonzero(values))
        tensors += 1
        total += len(values)
    assert total == expected_parameters and nonzero_b > 0
    return {'parameters': total, 'tensors': tensors, 'nonzero_lora_B_values': nonzero_b,
            'all_values_finite': True, 'base_weights_absent': True}


def main():
    import numpy as np
    from rerank_postprocess_policies import POLICIES, rerank
    from evidence_retrieval_metrics import coverage
    manifest = read(RAW / 'sync-manifest.json')
    assert all(sha(RAW / n) == h for n, h in manifest['files'].items())
    campaign = read(RAW / 'campaign-summary.json')
    plan = read(RAW / 'plan.json')
    assert campaign['status'] == 'completed' and campaign['protected_unchanged']
    assert campaign['serial_training_order'] == IDS
    assert all(campaign['phases'][i]['end'] <= campaign['phases'][i+1]['begin'] for i in range(2))
    assert all(campaign[n] == 0 for n in ['external_llm_calls', 'generation_calls', 'qa_calls', 'production_changes'])
    assert plan['status'] == 'frozen' and plan['unit_tests_passed'] == 13
    for name, digest in plan['data_sha256'].items(): assert sha(RAW / name) == digest
    for name, digest in plan['protected_sha256'].items():
        if name.startswith(('src/', 'scripts/', 'services/')):
            assert sha(RAW / 'source-snapshot' / name) == digest
        elif name.startswith(RAW.relative_to(ROOT).as_posix() + '/'):
            assert sha(ROOT / name) == digest
    local_source_match = {}
    local_source_semantic_match = {}
    for name,digest in plan['source_inputs'].items():
        assert sha(RAW/'source-snapshot'/name) == digest
        local_source_match[name] = (ROOT/name).exists() and sha(ROOT/name) == digest
        local_source_semantic_match[name] = (ROOT/name).exists() and read(ROOT/name) == read(RAW/'source-snapshot'/name)
    assert sha(ROOT / 'scripts/rerank_postprocess_policies.py') == plan['protected_sha256']['scripts/rerank_postprocess_policies.py']
    assert sha(BASE / 'summary.json') == plan['protected_sha256'][str((BASE/'summary.json').relative_to(ROOT)).replace('\\','/')]
    cp = ROOT / 'var/rag/corpora/kg_20261124_aviation_v7_charts_final'
    for f in ['rag_chunks.v1.jsonl', 'bge-m3.npy']:
        name = (cp / f).relative_to(ROOT).as_posix()
        assert sha(cp / f) == plan['protected_sha256'][name]
    records = {r['chunk_id']: r for r in map(json.loads, (cp/'rag_chunks.v1.jsonl').read_text(encoding='utf8').splitlines())}
    positions = {cid: j for j, cid in enumerate(records)}
    vectors = np.load(cp / 'bge-m3.npy', mmap_mode='r')
    dev, tests = read(RAW/'dev-pools.json'), read(RAW/'test-pools.json')
    cases = dev + tests
    assert collections.Counter(u['cohort'] for u in cases) == {'dev':72, 'holdout120':120, 'old113':113}
    base_rows = {p['id']: read(BASE/(p['id']+'-results.json'))['cases'][:] for p in POLICIES}
    for u in dev:
        scores = read(RAW/'dev-base-scores'/(u['id']+'.json'))['scores']
        order = {c:j for j,c in enumerate(u['pool'])}
        ranked = sorted(u['pool'], key=lambda c:(-scores[c], order[c]))
        v = np.asarray(vectors[[positions[c] for c in ranked]], dtype=np.float64)
        sim = np.clip(v@v.T, -1, 1)
        for p in POLICIES:
            ranked, _ = rerank(p, query=u['question'], pool=u['pool'], scores=scores, records=records, similarities=sim)
            base_rows[p['id']].append({**u, 'ranking':ranked, 'metrics':coverage(ranked,u['gold'],u['groups'])})
    all_rows = {'untrained': base_rows}
    metrics = {'untrained': {pid:aggregate(rs) for pid,rs in base_rows.items()}}
    trained, summaries, adapter_checks = {}, {}, {}
    checked = 0
    for sid in IDS:
        trained[sid] = t = read(RAW/sid/'training.json')
        s = read(RAW/sid/'eval/summary.json')
        summaries[sid] = s
        assert t['status'] == s['status'] == 'completed' and t['optimizer_steps'] == 35
        assert t['train_queries'] == 280 and t['settings'] == plan['settings']
        assert s['cohorts'] == {'dev':72, 'holdout120':120, 'old113':113}
        assert s['fresh_forward_pairs'] == 30500 and not s['test_used_for_checkpoint_selection']
        assert s['identity']['dev_pools'] == sha(RAW/'dev-pools.json')
        assert s['identity']['test_pools'] == sha(RAW/'test-pools.json')
        assert s['identity']['adapter_files'] == t['adapter_files_sha256']
        assert s['serial_order'] == [p['id'] for p in POLICIES]
        assert all(s[n] == t[n] == 0 for n in ['generation_calls','external_llm_calls','production_changes'])
        for name,digest in t['adapter_files_sha256'].items(): assert sha(RAW/sid/'adapter-last'/name) == digest
        adapter_checks[sid] = verify_adapter(RAW/sid/'adapter-last/adapter_model.safetensors', t['trainable_parameters'])
        rows = {p['id']:read(RAW/sid/'eval'/(p['id']+'-results.json'))['cases'] for p in POLICIES}
        index = {pid:{r['id']:r for r in rs} for pid,rs in rows.items()}
        for u in cases:
            entry = read(RAW/sid/'eval/scores'/(u['id']+'.json'))
            assert entry['identity'] == s['identity'] and entry['pool'] == u['pool']
            assert entry['gold'] == u['gold'] and entry['question'] == u['question']
            pool, scores = u['pool'], entry['scores']
            assert len(pool) == len(set(pool)) == 100 and set(scores) == set(pool)
            order = {c:j for j,c in enumerate(pool)}
            ranked = sorted(pool, key=lambda c:(-scores[c],order[c]))
            v = np.asarray(vectors[[positions[c] for c in ranked]], dtype=np.float64)
            sim = np.clip(v@v.T,-1,1)
            for p in POLICIES:
                r = index[p['id']][u['id']]
                expected, activation = rerank(p,query=u['question'],pool=pool,scores=scores,records=records,similarities=sim)
                assert expected == r['ranking'] and activation == r['activation']
                assert r['metrics'] == coverage(expected,u['gold'],u['groups'])
                assert r['gold_ranks'] == {c:expected.index(c)+1 if c in expected else None for c in u['gold']}
                assert r['metrics']['100'] == coverage(pool,u['gold'],u['groups'])['100']
                checked += 1
        metrics[sid] = {pid:aggregate(rs) for pid,rs in rows.items()}
        for p, ps in zip(POLICIES,s['policies'],strict=True):
            pid = p['id']
            assert ps['policy'] == p and ps['metrics'] == metrics[sid][pid]
            for h in ['dev','holdout120','old113']:
                for k in DEPTHS:
                    assert ps['untrained_same_policy_metrics'][h][str(k)]['complete_cases'] == metrics['untrained'][pid][h][str(k)]['complete_cases']
            assert ps['paired_against_untrained_same_policy'] == pairs(rows[pid],{r['id']:r for r in base_rows[pid]})
            assert ps['paired_against_adapted_reranker'] == pairs(rows[pid],index['reranker'])
        assert all(s['policies'][j]['serial_end'] <= s['policies'][j+1]['serial_begin'] for j in range(4))
        eligible = all(metrics[sid]['reranker']['dev'][str(k)]['complete_cases'] >= metrics['untrained']['reranker']['dev'][str(k)]['complete_cases'] for k in [5,10])
        assert eligible == s['dev_raw_top5_top10_not_worse']
        all_rows[sid] = rows
        print(json.dumps({'verified_scheme':sid, 'rankings_checked':checked}),flush=True)
    train = [json.loads(l) for l in (RAW/'train.jsonl').read_text(encoding='utf8').splitlines()]
    validation = [json.loads(l) for l in (RAW/'dev.jsonl').read_text(encoding='utf8').splitlines()]
    assert len(train) == 280 and len(validation) == 72
    ta = {c for g in train for c in g['equivalent_ids']}
    da = {c for g in validation for c in g['equivalent_ids']}
    test_ids = {c for u in tests for c in u['gold']}
    assert not ta & da and not (ta|da) & test_ids
    assert not {n['chunk_id'] for g in train+validation for n in g['negatives']} & (ta|da|test_ids)
    assert sum(len(g['gold']) for g in train) == 405 and sum(len(g['negatives']) for g in train) == 1120
    negatives = [n for g in train for n in g['negatives']]
    data_profile = {
        'train_categories':dict(collections.Counter(g['category'] for g in train)),
        'dev_categories':dict(collections.Counter(g['category'] for g in validation)),
        'train_gold_count_histogram':dict(collections.Counter(len(g['gold']) for g in train)),
        'dev_gold_count_histogram':dict(collections.Counter(len(g['gold']) for g in validation)),
        'train_positive_sources':dict(collections.Counter(records[c]['source_id'] for g in train for c in g['gold'])),
        'train_negative_bge_rank_quantiles':{str(q):float(np.quantile([n['bge_rank'] for n in negatives],q)) for q in [.0,.25,.5,.75,1.]},
        'train_negative_kind_counts':dict(collections.Counter(n['kind'] for n in negatives)),
        'negative_relevance_status':'weak screened labels; retrieval rank is not a confirmed reranker-hard label',
        'historical_input_source_of_truth':'frozen server source-snapshot; local historical files were not overwritten',
        'local_historical_input_hash_match':local_source_match,
        'local_historical_input_json_semantic_match':local_source_semantic_match,
        'train_dev_test_evidence_overlap':0}
    decision = {'checkpoint_selection':'fixed epoch1 adapter-last; no holdout tuning',
                'dev_eligible_schemes':[sid for sid in IDS if summaries[sid]['dev_raw_top5_top10_not_worse']],
                'eligible_with_old113_no_top5_top10_case_loss':[],
                'old113_case_loss_against_historical_untrained_rank_rrf':{},
                'production_changes':0, 'automatic_promotion':False,
                'limits':['Evidence coverage is not answer accuracy.',
                          '280 training questions, one seed, one epoch; this is a pilot, not a hyperparameter search.',
                          'Negatives are mechanically screened weak labels, binary-loss weight0.5; 32 reviewed samples do not certify all1120.',
                          'The fixed test was already baseline-evaluated; it is not a pristine never-viewed benchmark.',
                          'Training and test share a corpus and broad topics; duplicate evidence and near-queries are shielded, not whole documents.',
                          'Same100 candidates for every score/model; pool misses cannot be repaired by reranking.',
                          'Fresh old113 adapter scoring; untrained old113 uses existing frozen scores.']}
    for sid in IDS:
        decision['old113_case_loss_against_historical_untrained_rank_rrf'][sid] = {
            pid:pairs(rows,{r['id']:r for r in base_rows['rank_rrf']})['old113']
            for pid,rows in all_rows[sid].items()}
        for ps in summaries[sid]['policies']:
            if summaries[sid]['dev_raw_top5_top10_not_worse'] and not any(ps['paired_against_untrained_same_policy']['old113'][str(k)]['lost'] for k in [5,10]):
                decision['eligible_with_old113_no_top5_top10_case_loss'].append({'scheme':sid,'policy':ps['policy']['id']})
    decision['recommendation'] = 'Keep the current production model/configuration; all15 combinations lose at least one old113 Top5/Top10 case against the historical untrained rank_rrf reference.'
    assert all(any(v[str(k)]['lost'] for k in [5,10]) for d in decision['old113_case_loss_against_historical_untrained_rank_rrf'].values() for v in d.values())
    analysis = {
        'status':'completed_offline_pilot',
        'observations':[
            'P3 raw reranker: holdout120 Top5 69→74, Top10 89→94; dev raw Top5 61→62 and Top10 stays66.',
            'P1 plus RRF: holdout120 Top5 74→79, Top10 89→92; old113 Top5 109→108, Top10 stays111 but one case is lost and another gained.',
            'Every adapted RRF model has old113 Top5 108 versus untrained RRF109.',
            'MMR variants have no losses against their own weaker baseline, but still lose old cases against untrained RRF.',
            'Candidate100 completeness is109/120 for every model/policy;11 pool misses remain.'],
        'data_diagnosis':{'negative_bge_rank_median':data_profile['train_negative_bge_rank_quantiles']['0.5'],
                          'negative_label_kind':'all1120 are mechanically_screened_off_topic; not confirmed same-topic model-hard negatives',
                          'interpretation':'This is a likely limitation of training signal; the experiment does not establish it as the sole cause.'},
        'next_steps_in_priority_order':[
            'Within training queries only, mine the fixed Top100 and high original-reranker-score false positives; manually check relevance and partial evidence. Keep all test questions and gold/aliases protected.',
            'Add non-test project-background training examples and graded partial/full evidence labels; retain the old113 evaluation guard.',
            'Strengthen the grouped multi-evidence validation split before any further hyperparameter search; use a fresh untouched test for the next final comparison.',
            'Only after data revision, repeat the most promising objectives with multiple seeds; do not tune on this holdout120.'],
        'already_fixed_or_controlled':[
            'Historical broad labels, quote drift, equations and suspicious weak negatives screened before training; superseded draft manifests preserved.',
            'Native RAG-FiT processing reused, original completion-SFT loss replaced with explicit relevance losses in separate environment.',
            'Original BM25, fixed candidate100, test isolation, fixed checkpoints and serial objectives/policies preserved.',
            'No generation/API calls or production changes; all adapters and complete rankings retained.']}
    subgroups = {sid:{pid:{cat:{str(k):{'complete_cases':sum(r['metrics'][str(k)]['all_evidence'] for r in rs),'cases':len(rs)} for k in DEPTHS}
                 for cat in sorted({r['category'] for r in rows if r['cohort']=='holdout120'})
                 for rs in [[r for r in rows if r['cohort']=='holdout120' and r['category']==cat]]}
                 for pid,rows in policies.items()} for sid,policies in all_rows.items()}
    OUT.mkdir(parents=True,exist_ok=True)
    for n in manifest['files']:
        source = RAW/n
        # Include active data/adapters/audit, no training environment or original base model.
        target = OUT/n
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    shutil.copy2(RAW/'sync-manifest.json',OUT/'server-sync-manifest.json')
    save(OUT/'metrics.json',metrics); save(OUT/'decision.json',decision); save(OUT/'subgroups.json',subgroups)
    save(OUT/'data-profile.json',data_profile)
    save(OUT/'analysis.json',analysis)
    save(OUT/'delivery-validation.json',{'status':'passed','independent_rankings_metrics_checked':checked,
         'adapter_checks':adapter_checks,'serial_phases_nonoverlap':True,'all_candidate100_sets_unchanged':True,
         'train_dev_test_shield_verified':True,'server_protected_hashes_verified':True,'unit_tests_passed':13,
         'generation_calls':0,'external_llm_calls':0,'qa_calls':0,'production_changes':0})
    headers='<tr><th>模型</th><th>策略</th>'+''.join('<th>Top'+str(k)+'</th>' for k in DEPTHS)+'</tr>'
    def table(cohort):
        body=[]
        for sid in ['untrained',*IDS]:
            for p in POLICIES:
                m=metrics[sid][p['id']][cohort]
                body.append('<tr><th>'+esc(NAMES[sid])+'</th><td>'+esc(p['label'])+'</td>'+''.join('<td>'+str(m[str(k)]['complete_cases'])+'/'+str(m[str(k)]['cases'])+'</td>' for k in DEPTHS)+'</tr>')
        return '<table>'+headers+''.join(body)+'</table>'
    paired=[]
    for sid in IDS:
        for ps in summaries[sid]['policies']:
            ref=decision['old113_case_loss_against_historical_untrained_rank_rrf'][sid][ps['policy']['id']]
            paired.append('<tr><th>'+esc(NAMES[sid])+'</th><td>'+esc(ps['policy']['label'])+'</td>'+''.join('<td>'+str(len(ps['paired_against_untrained_same_policy'][h][str(k)]['gained']))+' / '+str(len(ps['paired_against_untrained_same_policy'][h][str(k)]['lost']))+'</td>' for h in ['holdout120','old113'] for k in [5,10])+'<td>'+str(len(ref['5']['lost']))+' / '+str(len(ref['10']['lost']))+'</td></tr>')
    cards=[]
    for u in tests:
        cid=u['id']; loss=False; rows=[]
        for sid in ['untrained',*IDS]:
            for p in POLICIES:
                r=next(r for r in all_rows[sid][p['id']] if r['id']==cid)
                b=next(r for r in all_rows['untrained'][p['id']] if r['id']==cid)
                loss |= any(b['metrics'][str(k)]['all_evidence'] and not r['metrics'][str(k)]['all_evidence'] for k in [5,10])
                ranks='、'.join(c[-8:]+':'+str(r['ranking'].index(c)+1 if c in r['ranking'] else '缺失') for c in u['gold'])
                rows.append('<tr><td>'+esc(NAMES[sid])+'</td><td>'+esc(p['label'])+'</td><td>'+esc(ranks)+'</td>'+''.join('<td>'+('完整' if r['metrics'][str(k)]['all_evidence'] else '缺证据')+'</td>' for k in [5,10,100])+'</tr>')
        gold=''.join('<li><code>'+esc(c)+'</code> · '+esc(records[c]['source_id'])+' · '+esc(' / '.join(records[c]['section_path']))+'</li>' for c in u['gold'])
        cards.append('<article data-cohort="'+u['cohort']+'" data-loss="'+str(loss).lower()+'"><h3>'+esc(cid)+'</h3><p>'+esc(u['question'])+'</p><ul>'+gold+'</ul><details><summary>展开原始模型及15组排名</summary><div class=scroll><table><tr><th>模型</th><th>策略</th><th>各gold排名</th><th>Top5</th><th>Top10</th><th>Top100</th></tr>'+''.join(rows)+'</table></div></details></article>')
    training_rows=''.join('<tr><th>'+esc(NAMES[sid])+'</th><td>'+str(trained[sid]['optimizer_steps'])+'</td><td>'+format(trained[sid]['loss_mean'],'.4f')+'</td><td>'+format(trained[sid]['seconds']/60,'.1f')+'</td><td>'+str(summaries[sid]['dev_raw_top5_top10_not_worse'])+'</td></tr>' for sid in IDS)
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reranker三组LoRA串行实验</title><style>body{font:15px/1.65 system-ui;margin:32px auto;max-width:1280px;padding:0 18px;color:#243449;background:#f7f9fc}h1,h2{line-height:1.3}section,article{background:white;border:1px solid #dbe3ed;border-radius:10px;padding:18px;margin:18px 0}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #dbe3ed;text-align:left;padding:7px}th{background:#edf3fa}code{font-size:12px;overflow-wrap:anywhere}.scroll{overflow:auto}select,input{padding:9px;margin:8px}.hidden{display:none}a{color:#1357a5}</style>
<h1>Reranker微调与五策略串行复测</h1><p>2026-11-25 · Qwen3-Reranker-0.6B · v7语料 · 离线证据评测</p>
<section><h2>结论与下一步</h2><p><strong>暂不替换生产模型。</strong>P3纯重排的新题Top5/Top10从69/89升到74/94；P1＋RRF从74/89升到79/92。但三组适配模型＋RRF的旧题Top5均从109降到108；P1/P3旧题Top10总数虽仍111，均存在1道退化与1道新增命中。15个组合相对历史未微调RRF都至少有一道旧题Top5或Top10退化。</p><p>负例BGE排名中位数为377.5，全部是机械筛过的离题弱标签，尚未形成经确认的同主题困难负例。这是训练信号可能受限的原因，当前实验不能证明它是唯一原因。下一轮优先在训练题Top100中人工确认高分易混淆负例，补充不与测试重叠的项目背景样本，再用多证据验证集、多seed检验；不把本轮测试错误直接转成训练样本。</p><p><a href="analysis.json">问题、已采取措施和下一步记录</a></p></section>
<section><h2>实验范围</h2><p>从420道历史航空题中筛选280道训练题、72道验证题，剔除68道。训练含405个正例片段、1120个弱负例。新120题和旧113题隔离作测试，未用于训练、早停、参数或检查点选择。测试此前已跑过基线，因此属于固定留出比较，不能称为从未查看的盲测。</p><p><a href='train.jsonl'>训练280题</a> · <a href='dev.jsonl'>验证72题</a> · <a href='data-profile.json'>题型与负例分布</a></p><p>先P1二分类，再P2多正例列表，最后P3弱正例配对；每组从同一原始模型独立开始，完成该组训练和五策略复测后才开始下一组。统一1 epoch、35次更新、seed20261125、LoRA r8/alpha16、q_proj/v_proj、学习率2e-5、最长2048 token。只更新1,146,880个适配参数。</p><p>原BM25和融合候选固定：BGE:BM25:exact=2:1:1、RRF k40、来源boost2.2、200→100。此为既有实验池，并非生产配置变更。每组重新评分305×100对，包括72验证题、120新题、113旧题；再依次执行五种后处理。没有外部LLM调用、问答生成、模型上传或生产变更。</p></section>
<section><h2>RAG-FiT如何复用</h2><p>实际复用了现有RAG-FiT的LocalLoader、TextPrompter、OutputData，输出1525行并逐行校验原始Qwen提示与yes/no标签。它原有训练入口面向回答生成SFT，直接使用会改变训练目标；本轮相关性损失与LoRA训练使用服务器现成scat-rag-train环境，旧RAG-FiT项目及环境保持原样。</p><p><a href="https://github.com/IntelLabs/RAG-FiT">官方RAG-FiT</a> · <a href="https://huggingface.co/Qwen/Qwen3-Reranker-0.6B">官方Qwen reranker</a> · <a href="plan.json">冻结计划和哈希</a> · <a href="processing-reranker.yaml">RAG-FiT处理配置</a></p></section>
<h2>新120题 · 完整证据覆盖</h2><p>分子为TopK内找到全部冻结gold片段的题数；这是证据覆盖率，不是问答准确率。Top100反映候选池上限，同一题的五策略和三模型候选集合保持相同。</p><div class=scroll><!--holdout--></div>
<h2>旧113题 · 全量适配模型重新评分</h2><p>原始模型沿用冻结基线分数；三个适配模型均重新执行113×100对前向评分。</p><div class=scroll><!--old--></div>
<h2>验证72题 · 资格判断</h2><div class=scroll><!--dev--></div>
<section><h2>逐题得失与资格</h2><p>与未微调的同一策略对比。下表为新增完整题 / 新增缺证据题，避免净增掩盖退化。预设资格要求原始reranker策略在验证集Top5/Top10均不下降；旧113题逐题退化另作上线门槛。通过同策略对照不代表超过未微调RRF；表中另列该历史参照的逐题退化。本轮不自动上线，也不根据新120题最高分重新选检查点或调参。</p><div class=scroll><table><tr><th>模型</th><th>策略</th><th>新题Top5 得/失</th><th>新题Top10 得/失</th><th>旧题Top5 得/失</th><th>旧题Top10 得/失</th><th>相对未微调RRF旧题Top5/10失</th></tr><!--paired--></table></div><p>验证资格通过：<!--eligible-->。满足该资格且旧113题Top5/Top10逐题无新增退化的组合：<!--safe-->。</p><p><a href="decision.json">资格与解释限制</a> · <a href="metrics.json">全部Top指标</a> · <a href="subgroups.json">新题分类型</a></p></section>
<section><h2>数据质量与复现</h2><p>按共享证据及近似问题连通组划分训练/验证；屏蔽测试gold及引文兼容片段、重复文本和近似问题。负例排除已知正例、相邻片段、同章节、重叠领域主题及高相似片段；二分类项对负例使用0.5权重，P2/P3排序项按冻结公式计算。抽查32条并核验可疑原文后保留，但这些仍是机械筛选的弱标签，不能当作1120条独立专家确认的负例。题集以合成航空题为主，训练与测试共享语料和大主题，不能代表所有真实用户提问。</p><p>13项契约测试、真实一步训练、保存重载和评分冒烟通过。交付时独立重算4575份完整排序及指标，核对每题100候选不变、15组策略结果和串行时间区间；检查三个适配器只有LoRA权重、数值有限且B矩阵实际更新。原始语料、向量、BM25、配置、基座文件和旧RAG-FiT源文件哈希保持一致。</p><table><tr><th>模型</th><th>优化步数</th><th>平均训练loss</th><th>训练分钟</th><th>验证资格</th></tr><!--training--></table><p>三种loss定义不同，不直接比较loss数值高低；单seed、单epoch为小样本试验。11道新题在候选Top100已缺完整证据，单靠重排无法修复。</p><p><a href="delivery-validation.json">独立复核</a> · <a href="negative-sample-review.json">32条负例抽查</a> · <a href="exclusions.json">排除记录</a> · <a href="source-snapshot/scripts/train_reranker_ft.py">训练脚本</a> · <a href="source-snapshot/scripts/evaluate_reranker_ft.py">复测脚本</a></p></section>
<h2>逐题查看</h2><select id=cohort><option value=holdout120>新120题</option><option value=old113>旧113题</option><option value=all>全部233题</option></select><select id=mode><option value=all>全部题目</option><option value=loss>任一适配策略出现Top5/10退化</option></select><input id=query placeholder="搜索题目、题号、证据ID"><span id=count></span><!--cards--><script>const cards=[...document.querySelectorAll('article')];function update(){let h=document.querySelector('#cohort').value,m=document.querySelector('#mode').value,q=document.querySelector('#query').value.toLowerCase();cards.forEach(c=>c.classList.toggle('hidden',(h!=='all'&&c.dataset.cohort!==h)||(m==='loss'&&c.dataset.loss!=='true')||(q&&!c.textContent.toLowerCase().includes(q))));document.querySelector('#count').textContent='显示 '+cards.filter(c=>!c.classList.contains('hidden')).length+' / 233'}document.querySelectorAll('select').forEach(e=>e.onchange=update);document.querySelector('#query').oninput=update;update();</script></html>'''
    replacements={'holdout':table('holdout120'),'old':table('old113'),'dev':table('dev'),'paired':''.join(paired),
      'eligible':'、'.join(NAMES[sid] for sid in decision['dev_eligible_schemes']) or '无',
      'safe':'、'.join(NAMES[x['scheme']]+' / '+next(p['label'] for p in POLICIES if p['id']==x['policy']) for x in decision['eligible_with_old113_no_top5_top10_case_loss']) or '无',
      'training':training_rows,'cards':''.join(cards)}
    for key,value in replacements.items(): page=page.replace('<!--'+key+'-->',value)
    assert '<!--' not in page
    (OUT/'REPORT.html').write_text(page,encoding='utf8')
    (OUT/'delivery-tools').mkdir(exist_ok=True)
    for n in ['report_reranker_ft.py','sync_reranker_ft.py']:
        shutil.copy2(ROOT/'scripts'/n,OUT/'delivery-tools'/n)
    save(OUT/'manifest.json',{'files':{p.relative_to(OUT).as_posix():sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='manifest.json'}})
    archive=OUT.with_suffix('.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OUT.rglob('*')):
            if f.is_file(): z.write(f,f.relative_to(OUT.parent).as_posix())
    print(json.dumps({'report':str(OUT/'REPORT.html'),'zip_sha256':sha(archive),'rankings_checked':checked,'decision':decision},ensure_ascii=False))


if __name__=='__main__':main()
