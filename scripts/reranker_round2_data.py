"""Offline Top100 mining and local 4B teacher review. Does not start training."""
import collections
import gc
import json
import math
import sys
import time
from pathlib import Path

from reranker_ft_common import ROOT, read, save, sha, norm, offline, no_generation, corpus, tags

OUT = ROOT / 'var/validation/20261125-reranker-round2'
OLD = ROOT / 'var/validation/20261125-reranker-lora-v1'


def main():
    offline()
    no_generation()
    import numpy as np
    import torch
    from rag_retrieval.production import Retriever, load_config
    from rag_retrieval.enhancements import rank_exact_identifier_matches
    from rag_retrieval.runtime import M8_RRF_PROFILE
    from fusion_policy_experiments import POLICIES, select
    from rag_retrieval.rerank_comparison import Qwen3Reranker
    plan = read(OUT / 'prepared-plan.json')
    assert not (OUT / 'frozen-plan.json').exists()
    assert all(sha(ROOT / n) == h for n, h in plan['source_hashes'].items())
    cfg, chunks, rs = corpus()
    train = [json.loads(x) for x in (OLD / 'train.jsonl').read_text().splitlines()]
    dev = [json.loads(x) for x in (OLD / 'dev.jsonl').read_text().splitlines()]
    tests = read(OLD / 'test-pools.json')
    background = [json.loads(x) for x in (OUT / 'background-candidates.jsonl').read_text().splitlines()]
    protected = {c for g in train + dev for c in g['equivalent_ids']}
    protected.update(c for u in tests for c in u['gold'])
    for aliases in read(ROOT / 'var/validation/20261125-rag-holdout120-rerank-serial-v1/quote-compatible-alternatives.json').values():
        protected.update(aliases)
    protected_hashes = {rs[c]['text_sha256'] for c in protected}
    engine = Retriever(ROOT, load_config(ROOT, 'services/rag/qwen3-production-v1.json'))
    ids = list(rs)
    vectors = np.asarray(engine.index.corpus_vectors)
    qv = engine.index.model.encode([g['question'] for g in train + background + dev + tests],
        batch_size=8, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    excluded = []
    extra = []
    guard_queries = train + dev + tests
    guard_vectors = np.concatenate([qv[:len(train)], qv[len(train)+len(background):]], axis=0)
    for j, row in enumerate(background):
        similarity = guard_vectors @ qv[len(train)+j]
        nearest = int(similarity.argmax())
        if float(similarity[nearest]) >= .85:
            excluded.append({'id': row['id'], 'reason': 'near_historical_query', 'cosine': float(similarity[nearest]), 'nearest': guard_queries[nearest].get('id')})
            continue
        protected.update(row['equivalent_ids'])
        extra.append({**row, 'groups': [row['gold']], 'category': 'project_background',
                      'split': 'train', 'background': True, 'qvector_index': len(train)+j})
    assert extra, 'No independent background sample survived; do not weaken isolation'
    cases = [{**g, 'background': False, 'qvector_index': j} for j, g in enumerate(train)] + extra
    protected_hashes.update(rs[c]['text_sha256'] for g in extra for c in g['equivalent_ids'])
    teacher_inputs = []
    for j, g in enumerate(cases, 1):
        target = OUT / 'mined' / (g['id']+'.json')
        if target.exists():
            teacher_inputs.append(read(target))
            continue
        sim = vectors @ qv[g['qvector_index']]
        dense = [ids[int(x)] for x in np.argsort(-sim, kind='stable')[:200]]
        lexical = [c for c, _ in engine.index.bm25.rank(' '.join(g['question'].split()), limit=200)]
        exact = [c for c, _ in rank_exact_identifier_matches(g['question'], engine.index.chunks, limit=200)]
        pool = select(next(p for p in POLICIES if p['id']=='weighted2'), dense, lexical, exact,
                      engine.index.chunks_by_id, ' '.join(g['question'].split()), M8_RRF_PROFILE)
        assert len(pool) == len(set(pool)) == 100
        score_ids = list(dict.fromkeys(pool + g['gold'] + [n['chunk_id'] for n in g.get('negatives', [])]))
        base, info = engine.reranker.score(g['question'], [chunks[c].retrieval_text() for c in score_ids])
        if info['truncated_pairs']:
            excluded.append({'id':g['id'], 'reason':'mined_pair_truncation'})
            continue
        scores = dict(zip(score_ids, base))
        topic = tags(g['question']) | set().union(*(tags(rs[c]['text']) for c in g['gold']))
        def allowed(cid):
            r = rs[cid]
            if cid in protected or r['text_sha256'] in protected_hashes:
                return False
            # Literal alternative evidence must never be converted into a negative.
            if any(e.get('quote') and e['quote'] in r['text'] for e in g.get('evidence', [])):
                return False
            return True
        hard = [c for c in sorted(pool, key=lambda c:-scores[c]) if allowed(c)
                and bool(topic & tags(rs[c]['text']))][:12]
        easy = [n['chunk_id'] for n in g.get('negatives', []) if allowed(n['chunk_id'])]
        if g['background']:
            easy = [c for c in reversed(pool) if allowed(c)][:4]
        selected = list(dict.fromkeys(g['gold'] + hard + easy))
        entry = {'group':g, 'pool':pool, 'base_scores':scores, 'teacher_ids':selected,
                 'top100_hard_candidates':hard, 'base_info':info}
        save(target, entry)
        teacher_inputs.append(entry)
        save(OUT/'progress.json', {'stage':'mine_training_top100','completed':j,'total':len(cases)})
    save(OUT/'background-semantic-exclusions.json', excluded)
    del engine, vectors, qv
    gc.collect()
    torch.cuda.empty_cache()
    manifest = read(ROOT/'services/rag/qwen3-4b-download.json')['models'][0]
    teacher_path = Path(manifest['destination'])
    assert all(sha(teacher_path / x['path']) == x['sha256'] for x in manifest['files'])
    teacher = Qwen3Reranker(str(teacher_path), batch_size=2, max_length=2048)
    accepted = []
    review = []
    for j, entry in enumerate(teacher_inputs, 1):
        g = entry['group']
        path = OUT/'teacher'/(g['id']+'.json')
        if path.exists():
            judged = read(path)
        else:
            scores, info = teacher.score(g['question'], [chunks[c].retrieval_text() for c in entry['teacher_ids']])
            assert not info['truncated_pairs']
            judged = {'id':g['id'],'scores':dict(zip(entry['teacher_ids'],scores)),
                      'teacher_manifest_sha256':sha(ROOT/'services/rag/qwen3-4b-download.json'), 'info':info}
            save(path, judged)
        negative = [c for c in entry['teacher_ids'] if c not in g['gold'] and judged['scores'][c] <= .1]
        hard = [c for c in entry['top100_hard_candidates'] if c in negative]
        # Every admitted question must actually have at least one reviewed Top100 negative.
        if not hard or len(negative)<2:
            excluded.append({'id':g['id'],'reason':'insufficient_teacher_rejected_top100_negatives'})
            continue
        chosen = (hard[:3]+[c for c in negative if c not in hard])[:4]
        if len(chosen)<2:
            continue
        ids_train = g['gold']+chosen
        groups = g['groups']
        grades = [2 if all(set(group)<={cid} for group in groups) else 1 for cid in g['gold']] + [0]*len(chosen)
        result = {**g, 'negatives':[{'chunk_id':c,'kind':'teacher_screened_same_topic_top100' if c in hard else 'teacher_screened_control',
                    'teacher_probability':judged['scores'][c],'base_probability':entry['base_scores'][c],
                    'candidate_rank':entry['pool'].index(c)+1 if c in entry['pool'] else None} for c in chosen],
                  'hard_negative_positions':[len(g['gold'])+i for i,c in enumerate(chosen) if c in hard],
                  'grades':grades, 'teacher_probabilities':[judged['scores'][c] for c in ids_train],
                  'original_probabilities':[entry['base_scores'][c] for c in ids_train],
                  'label_status':'teacher_screened_weak_negatives; positives from prior evidence annotations; assistant review sample required'}
        accepted.append(result)
        for c in chosen:
            if c in hard:
                review.append({'id':g['id'],'question':g['question'],'negative_id':c,
                    'teacher_probability':judged['scores'][c],'base_probability':entry['base_scores'][c],
                    'positive_texts':{x:rs[x]['text'] for x in g['gold']},'negative_text':rs[c]['text']})
        save(OUT/'progress.json', {'stage':'teacher_review','completed':j,'total':len(teacher_inputs),'admitted_queries':len(accepted)})
    assert len(accepted)>=60
    assert any(g['background'] for g in accepted), 'Background admission failed'
    assert not ({c for g in accepted for c in g['equivalent_ids']} & {c for g in dev for c in g['equivalent_ids']})
    assert not ({c for g in accepted for c in g['equivalent_ids']} & {c for u in tests for c in u['gold']})
    (OUT/'train-proposed.jsonl').write_text(''.join(json.dumps(g,ensure_ascii=False)+'\n' for g in accepted), encoding='utf8')
    review.sort(key=lambda x: (-x['base_probability'], x['id'], x['negative_id']))
    # Full contents are exported. Audit the most severe teacher/student disagreements.
    sample = review[:8]
    background_review = [x for x in review if x['id'].startswith('BG2-')][:4]
    sample += [x for x in background_review if x not in sample]
    save(OUT/'negative-review.json',sample)
    save(OUT/'mining-summary.json', {'status':'awaiting_assistant_sample_review','train_queries':len(accepted),
        'background_queries':sum(g['background'] for g in accepted),'negative_pairs':sum(len(g['negatives']) for g in accepted),
        'teacher_reviewed_pairs':sum(len(read(p)['scores']) for p in (OUT/'teacher').glob('*.json')),
        'negatives_are_expert_confirmed':False,'teacher_probability_is_not_calibrated_relevance':True,
        'source_hashes':plan['source_hashes'],'excluded':excluded,'external_api_calls':0,'production_changes':0})
    print(json.dumps(read(OUT/'mining-summary.json'),ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
