"""Prepare the authorized second-round experiment; never train or change production.

This stage is intentionally standard-library only. GPU teacher review, semantic
leakage checks and runtime validation are mandatory before data admission.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = ROOT / 'var/validation/20261125-reranker-lora-v1'
OUT = ROOT / 'var/validation/20261125-reranker-round2'
CORPUS = ROOT / 'var/rag/corpora/kg_20261124_aviation_v7_charts_final/rag_chunks.v1.jsonl'

# Authored from the actual unused source fragments, not from test questions.
# Each quote is checked literally. Semantic isolation remains a separate gate.
BACKGROUND = [
    ('kc1_3e84789849ce2df313186e400876de60',
     'GBAR 的行为条件向量 w 表示什么，推理时是否需要未来行为标签？',
     '`w` 是行为类别的软置信度，作为 GBAR 的条件向量。'),
    ('kc1_93b8bb2fa53ac58c1b13ff059aaa5f85',
     'GBAR 的行为嵌入模块由哪些组件构成？',
     '行为嵌入模块包含共享行为编码器、行为识别头和训练期 MFHA 辅助头。'),
    ('kc1_5301a700f246f8e0228cd40e7125c858',
     'GBAR 的 FiLM 残差块使用什么条件向量生成哪些调制参数？',
     '每个 FiLM 残差块从同一个行为向量 `w` 生成两组逐通道缩放和平移参数'),
    ('kc1_eb27866549b429631950a10f6d808cd7',
     'GBAR 的联合微调和推理是否对后验潜变量随机采样？',
     '联合微调和推理时固定使用 `z=mu_z`。'),
    ('kc1_6bca69c7f5a42844fcf2d7d9dd170972',
     'GBAR 的行为嵌入预训练结束后如何处置 MFHA 和行为编码器？',
     '完成后删除 MFHA，冻结行为编码器和识别头。'),
    ('kc1_d37b80bc1d12e4f061d266f3f75fab59',
     'GBAR 独立去噪预训练的噪声强度和训练轮数分别是多少？',
     '独立 GBAR 预训练 50 epochs，`sigma_pre=0.02`。'),
    ('kc1_16518f13ee64bc475f9c70aa3a70b893',
     'GBAR 应先划分完整轨迹还是先生成滑动窗口，模型按什么指标选择？',
     '模型选择依据验证集 MDE，测试集只用于最终评估。'),
    ('kc2_1ac29fcace14cf4e338d947425f068e3',
     'SCAT 周包中除了单航班 JSON 还包含哪两个公共文件？',
     '一个 `airspace.json` 和一个 `grib_meteo.json`'),
    ('kc2_868f9ab617fc2f9399846db0c4f8f47e',
     'SCAT 航路点 route[].eto 与 route[].is_ato 字段各表示什么？',
     'Estimated Time Over，预计过点时间'),
    ('kc2_e930e9ec0764ad6ca4b9893396c20a4f',
     'SCAT 如何关联 Malmö 与 Stockholm 两个中心的同一航班记录？',
     '航班使用 IFPLID 关联'),
    ('kc1_bc80b451af3462c50d936dbc993d26a0',
     '查看 GBAR 后台任务结果时 run_id 和 artifact_id 各起什么作用？',
     '成功任务通常会产生结果制品 `artifact_id`'),
    ('kc1_2fad53ac172b93ece0bff1d03ec800ec',
     '为什么系统优先提供航迹抽样和分页，而非整条原始轨迹？',
     '系统优先提供抽样、分页、统计和图表，以保证结果可读。'),
]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    assert not (OUT / 'frozen-plan.json').exists(), 'Do not modify a frozen experiment'
    records = {r['chunk_id']: r for r in map(json.loads, CORPUS.read_text(encoding='utf-8').splitlines())}
    old_plan = read(PREVIOUS / 'plan.json')
    assert digest(CORPUS) == old_plan['protected_sha256'][CORPUS.relative_to(ROOT).as_posix()]
    protected = set()
    for name in ['train.jsonl', 'dev.jsonl']:
        assert digest(PREVIOUS / name) == old_plan['data_sha256'][name]
        for row in map(json.loads, (PREVIOUS / name).read_text(encoding='utf-8').splitlines()):
            protected.update(row['equivalent_ids'])
    tests = read(PREVIOUS / 'test-pools.json')
    for row in tests:
        protected.update(row['gold'])
    aliases_path = ROOT / 'var/validation/20261125-rag-holdout120-rerank-serial-v1/quote-compatible-alternatives.json'
    for aliases in read(aliases_path).values():
        protected.update(aliases)
    protected_hashes = {records[c]['text_sha256'] for c in protected}
    accepted, excluded = [], []
    for index, (cid, question, quote) in enumerate(BACKGROUND, 1):
        source = records[cid]
        # Duplicate text and same-source quote aliases must also be shielded.
        equivalents = {c for c, r in records.items() if r['text_sha256'] == source['text_sha256'] or
                       (r['source_id'] == source['source_id'] and quote in r['text'])}
        reason = None
        if quote not in source['text']:
            reason = 'authored_quote_does_not_match_current_source'
        elif equivalents & protected or source['text_sha256'] in protected_hashes:
            reason = 'existing_train_dev_or_test_evidence_or_alias_overlap'
        row = {'id': f'BG2-20261125-{index:03d}', 'question': question, 'gold': [cid],
               'equivalent_ids': sorted(equivalents), 'source_id': source['source_id'],
               'text_sha256': source['text_sha256'], 'quote': quote,
               'review': 'assistant_authored_and_literal_quote_checked',
               'admission': 'pending_query_semantic_and_split_checks_not_training_data'}
        if reason:
            excluded.append({**row, 'reason': reason})
        else:
            accepted.append(row)
    (OUT / 'background-candidates.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in accepted), encoding='utf-8')
    (OUT / 'background-exclusions.json').write_text(json.dumps(excluded, ensure_ascii=False, indent=2), encoding='utf-8')
    plan = {
        'status': 'prepared_not_launched',
        'authorization': 'User requested this proposal plus two additional serial tests',
        'three_experiments': [
            {'id': 'a_distill_preserve', 'objective': 'relevance supervision + teacher soft-score distillation + original-model preservation',
             'teacher': 'existing pinned local Qwen3-Reranker-4B, verify files and fit before use'},
            {'id': 'b_graded_lambda', 'objective': 'top10 LambdaRank-weighted pairwise loss with independently reviewed graded relevance',
             'grades': {'0': 'irrelevant', '1': 'partial support', '2': 'direct support'},
             'warning': 'teacher relevance probability alone is not proof of partial/full evidence support'},
            {'id': 'c_dual_branch', 'objective': 'equal-weight rank fusion of original model and experiment A adapter',
             'rank_constant': 20, 'original_weight': 0.5, 'adapted_weight': 0.5,
             'training': 'reuse experiment A adapter; this is a separate inference/evaluation experiment, not a third loss'},
        ],
        'controls': ['untrained model', 'retrained P1 on same revised data', 'retrained P3 on same revised data'],
        'serial_order': ['data mining', 'teacher scoring', 'negative/graded-label review', 'freeze data and plan',
                         'P1 train and evaluate', 'P3 train and evaluate',
                         'A train and evaluate', 'B train and evaluate', 'C fusion and evaluate'],
        'initial_training_settings': old_plan['settings'],
        'data_requirements': [
            'Mine actual fixed Top100 pools for training queries only, including high original-reranker-score confusing candidates.',
            'Do not treat unannotated candidates as automatically irrelevant; protect partial support and alternative evidence.',
            'Keep teacher judgments distinct from assistant or human review; uncertain labels are excluded.',
            'Preservation examples must be independent of old113 test questions, gold, aliases and near-duplicate queries.',
            'Group evidence aliases and near queries before splitting; do not let teacher train on dev/test judgments.',
            'Background candidates require semantic rejection against all 233 held-out questions before admission.',
        ],
        'evaluation': {
            'historical_tests': {'holdout120': 120, 'old113': 113},
            'interpretation': 'previously viewed fixed comparisons, not untouched blind tests',
            'depths': [5, 10, 20, 50, 100], 'same_candidate100_for_every_model': True,
            'report': ['complete evidence coverage', 'paired gains and losses', 'per-category results',
                       'latency and memory', 'actual forward counts and fusion overhead'],
            'selection': 'use dev only; no checkpoint, threshold or fusion-weight tuning on test results',
            'production': 'no automatic promotion; old-case regressions must remain visible',
        },
        'source_hashes': {p.relative_to(ROOT).as_posix(): digest(p) for p in
                          [CORPUS, PREVIOUS / 'plan.json', PREVIOUS / 'train.jsonl',
                           PREVIOUS / 'dev.jsonl', PREVIOUS / 'test-pools.json', aliases_path]},
        'background_candidates_quote_and_alias_checked': len(accepted),
        'background_candidates_excluded': len(excluded),
        'new_training_started': False, 'external_api_calls': 0, 'production_changes': 0,
        'execution_gate': 'SSH connection, server resources, teacher provenance, semantic isolation, audited labels and objective tests all required',
    }
    (OUT / 'prepared-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': plan['status'], 'background_candidates': len(accepted), 'excluded': len(excluded),
                      'plan': str(OUT / 'prepared-plan.json')}, ensure_ascii=True))


if __name__ == '__main__':
    prepare()
